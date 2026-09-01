/** 内置演示数据：API 不可达时优雅降级使用（UI 会标注「演示数据」）。品牌文案一律读配置，无硬编码。 */
import { getConfig } from "./config";
import type { MemberInfo, NotificationItem, Order, TimelineItem, Ticket } from "./types";

export const DEMO_FLAG = "演示数据";

export function getDemoOrders(): Order[] {
  const brand = getConfig().brandName;
  return [
    {
      id: "ORD-20260820-001",
      title: `${brand}API 推送·专业版（年付）`,
      status: "服务中",
      checkIn: "2026-08-22",
      roomType: "API 推送·专业版",
      amount: 599,
    },
    {
      id: "ORD-20260815-002",
      title: "自选股池监测（≤50 只·月付）",
      status: "已完成",
      checkIn: "2026-08-15",
      roomType: "自选股池监测",
      amount: 128,
    },
  ];
}

export function getDemoMember(): MemberInfo {
  return {
    level: "专业版",
    points: 2680,
    benefits: ["决策日报 + 个股深度免费看", "API 推送 300 次/分钟", "自选股池监测 8 折", "专属合规咨询通道"],
    demo: true,
  };
}

export const demoTickets: Ticket[] = [
  {
    id: "TK-20260822-101",
    kind: "delivery",
    title: "数据服务：开通研报速递订阅（盘前 5 分钟）",
    status: "处理中",
    createdAt: "2026-08-22T19:42:00.000Z",
    slaDueAt: "2026-08-22T20:12:00.000Z",
  },
  {
    id: "TK-20260821-087",
    kind: "repair",
    title: "异常申报：纳斯达克行情快照延时异常",
    status: "已完成",
    createdAt: "2026-08-21T14:05:00.000Z",
  },
  {
    id: "TK-20260820-066",
    kind: "complaint",
    title: "投诉建议：决策日报推送延迟 40 分钟",
    status: "已受理",
    createdAt: "2026-08-20T09:18:00.000Z",
  },
];

export function demoTimeline(ticketId: string): TimelineItem[] {
  const base = Date.now() - 1000 * 60 * 42;
  return [
    {
      action: "created",
      actorType: "guest",
      actorId: "me",
      detail: "工单已提交，AI 前台已受理",
      createdAt: new Date(base).toISOString(),
    },
    {
      action: "assigned",
      actorType: "agent",
      actorId: "AI-Concierge",
      detail: "已派单至数据质量组（演示流转）",
      createdAt: new Date(base + 1000 * 60 * 5).toISOString(),
    },
    {
      action: "progress",
      actorType: "staff",
      actorId: "staff-0312",
      detail: `工单 ${ticketId} 处理中，数据质量官已介入核对`,
      createdAt: new Date(base + 1000 * 60 * 18).toISOString(),
    },
  ];
}

export function getDemoNotifications(): NotificationItem[] {
  return [
    {
      kind: "ticket.accepted",
      payload: { ticketId: "TK-20260822-101", title: "数据服务：开通研报速递订阅（盘前 5 分钟）" },
      createdAt: "2026-08-22T19:42:10.000Z",
      read: false,
    },
    {
      kind: "ticket.completed",
      payload: { ticketId: "TK-20260821-087", title: "异常申报：纳斯达克行情快照延时异常" },
      createdAt: "2026-08-21T15:30:00.000Z",
      read: true,
    },
    {
      kind: "member.benefit",
      payload: { title: "订阅权益到账", detail: "本月个股深度报告免费额度已生效" },
      createdAt: "2026-08-20T08:00:00.000Z",
      read: true,
    },
  ];
}

/** 关键词匹配的演示应答（用于 /c/chat 降级） */
export function demoChatAnswer(text: string): {
  intent: string;
  answer: string;
  confidence: number;
  citations: { documentTitle: string; heading: string; content: string }[];
  cards?: { kind: "order" | "member" | "catalog"; data: Record<string, unknown> }[];
} {
  const brand = getConfig().brandName;
  const t = text.toLowerCase();
  if (/净值|持仓|订单|订阅/.test(text)) {
    return {
      intent: "order.query",
      answer: "为您查到以下订阅订单：API 推送·专业版（年付）服务中。模拟盘最新净值与持仓见上方卡片，逐笔留痕可回放。",
      confidence: 0.92,
      citations: [
        {
          documentTitle: `${brand}服务说明`,
          heading: "净值与持仓查询",
          content: "小虎模拟盘以 100,000 美元虚拟资金起步，逐笔留痕；净值与持仓每日随《决策日报》披露。",
        },
      ],
      cards: [{ kind: "order", data: getDemoOrders()[0] as unknown as Record<string, unknown> }],
    };
  }
  if (/会员|积分|权益|专业版/.test(text)) {
    return {
      intent: "member.info",
      answer: "您当前是专业版，积分 2680。专业版权益包含决策日报 + 个股深度免费看、API 推送 300 次/分钟等，详情见下方会员卡。",
      confidence: 0.95,
      citations: [
        {
          documentTitle: `${brand}会员手册`,
          heading: "专业版权益",
          content: "专业版享决策日报 + 个股深度免费看、API 推送 300 次/分钟、自选股池监测 8 折。",
        },
      ],
      cards: [{ kind: "member", data: getDemoMember() as unknown as Record<string, unknown> }],
    };
  }
  if (/开仓|加仓|纪律|实盘|真实|建议/.test(t) || /开仓|加仓|纪律|实盘|真实|建议/.test(text)) {
    return {
      intent: "faq",
      answer:
        "开仓须同时满足 MRS*≥6、SHS≥7.5、TSS_final≥7.2，MRS*<4.0 禁止开仓——机械规则，模型无法越过。当前仅模拟盘运行，不做真实下单，不构成投资建议。",
      confidence: 0.88,
      citations: [
        {
          documentTitle: `${brand}交易纪律`,
          heading: "开仓硬逻辑与合规",
          content: "标准做多 MRS*≥6 且 SHS≥7.5 且 TSS_final≥7.2；仅模拟盘，不构成投资建议。",
        },
      ],
    };
  }
  return {
    intent: "fallback",
    answer: "这个问题我还在学习中，已为您转专人处理，稍后会有服务专员与您联系。您也可以直接描述需要的服务。",
    confidence: 0.42,
    citations: [],
  };
}
