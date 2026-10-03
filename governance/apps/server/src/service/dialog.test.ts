/**
 * server · dialog 意图/置信度纯函数单测（不触 DB）
 * M8：无已验证行业适配器时不猜测酒店/交易业务；工单进度仍为公共查询。
 * H5：置信度归一化三档边界（≥0.72 直答 / 0.45–0.72 附提示 / <0.45 拒答）。
 */
import { describe, expect, it } from "vitest";
import { classify, tierOfScore, ticketKindOf, CONFIDENCE_HIGH, CONFIDENCE_MEDIUM } from "./dialog.js";

describe("classify 意图规则（M8 与 base 同表）", () => {
  const cases: Array<[string, string, string?]> = [
    ["我要投诉房间太吵", "complaint"],
    ["查一下我的订单", "kb_qa"],
    ["我的会员积分还有多少", "kb_qa"],
    ["豪华大床房多少钱一晚", "kb_qa"],
    ["我的工单进度怎么样了", "biz_query", "query_ticket"],
    ["送站巴士几点发车", "kb_qa"],            // 含「送」但疑问句 → kb_qa 不建单
    ["系统什么情况下会开仓", "kb_qa"],
    ["空调坏了，帮我修一下", "kb_qa"],
    ["帮我送两瓶矿泉水", "kb_qa"],
    ["需要人工协助", "service_request"],
    ["附近地铁站怎么走", "kb_qa"],            // 无规则命中 → 默认 kb_qa（低置信拒答）
  ];
  for (const [text, intent, tool] of cases) {
    it(`「${text}」→ ${intent}${tool ? `/${tool}` : ""}`, () => {
      const r = classify(text);
      expect(r.intent).toBe(intent);
      if (tool) expect(r.tool).toBe(tool);
    });
  }
});

describe("ticketKindOf service_request → 工单类型", () => {
  it("未提供已验证行业适配器时使用通用 other，不猜测履约部门", () => {
    expect(ticketKindOf("空调坏了，帮我修一下")).toBe("other");
    expect(ticketKindOf("帮我送两瓶矿泉水")).toBe("other");
    expect(ticketKindOf("帮我安排一个安静点的房间")).toBe("other");
  });
});

describe("tierOfScore 置信度三档（H5，归一化 0..1）", () => {
  it("阈值边界", () => {
    expect(CONFIDENCE_HIGH).toBe(0.72);
    expect(CONFIDENCE_MEDIUM).toBe(0.45); // 评测校准：区分度地板与拒答边界拉开
    expect(tierOfScore(0.95)).toBe("high");
    expect(tierOfScore(0.72)).toBe("high");
    expect(tierOfScore(0.71)).toBe("medium");
    expect(tierOfScore(0.45)).toBe("medium");
    expect(tierOfScore(0.44)).toBe("low");
    expect(tierOfScore(0)).toBe("low");
    expect(tierOfScore(undefined)).toBe("low");
  });
  it("越界输入归一化", () => {
    expect(tierOfScore(1.2)).toBe("high");
    expect(tierOfScore(-0.3)).toBe("low");
  });
});
