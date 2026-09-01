/**
 * 配置驱动模板化：企业接入只需替换 public/service-front.config.json
 * - 启动时 fetch 配置；失败/缺字段 → 内置 DEFAULT_CONFIG 兜底（永不白屏）
 * - 主题色运行时注入 CSS 变量（Tailwind v4 @theme 令牌全部走 var()，改色零改码）
 * - 文案支持 {brand} / {agent} 占位符
 */

export type TabKey = "chat" | "service" | "tickets" | "messages" | "me";

export interface QuickReply {
  label: string;
  /** 点击后直接发送的文本 */
  sendText?: string;
  /** 点击后跳转服务页并预填的工单 kind */
  serviceKind?: string;
}

export interface ServiceEntry {
  kind: string;
  title: string;
  desc: string;
  icon: string;
  sla: string;
  titlePlaceholder?: string;
}

export interface FrontConfig {
  brandName: string;
  agentName: string;
  /** 头像/Logo 字符（emoji 或单字） */
  logoText: string;
  theme: { primary: string; secondary: string };
  welcomeText: string;
  quickReplies: QuickReply[];
  serviceEntries: ServiceEntry[];
  /** 会员等级展示映射：后端 level → 前端展示文案 */
  memberLevels: Record<string, { label: string }>;
  /** 演示对话历史（首开非空·全场景运行态剧本；可选） */
  demoHistory?: Array<{ role: "user" | "ai"; text: string }>;
  /** 可关闭的底部 Tab */
  enableTabs: TabKey[];
  supportPhone: string;
}

export const DEFAULT_CONFIG: FrontConfig = {
  brandName: "老虎交易 · 小虎模拟盘",
  agentName: "小虎",
  logoText: "虎",
  theme: { primary: "#e9b558", secondary: "#45e0ff" },
  welcomeText:
    "您好，欢迎来到{brand}。我是 AI 服务前台{agent}，可以帮您查净值、查持仓、订阅数据服务、申报异常，也可以解答交易纪律与合规问题。当前仅模拟盘运行，不构成投资建议。",
  quickReplies: [
    { label: "查净值", sendText: "帮我查一下当前模拟盘净值" },
    { label: "数据服务", serviceKind: "delivery" },
    { label: "异常申报", serviceKind: "repair" },
    { label: "常见问题", sendText: "系统什么情况下会开仓？现在是实盘吗？" },
  ],
  serviceEntries: [
    { kind: "delivery", title: "数据服务", desc: "研报速递 / 个股深度 / 产业链监测订阅", icon: "订", sla: "开通后每日自动推送", titlePlaceholder: "例如：开通研报速递订阅" },
    { kind: "repair", title: "异常申报", desc: "行情 / 信号 / 账户 / 报表异常一键申报", icon: "报", sla: "加急 15 分钟内响应", titlePlaceholder: "例如：行情快照延时异常" },
    { kind: "complaint", title: "投诉建议", desc: "您的意见让我们更好", icon: "诉", sla: "值班负责人 2 小时内回复", titlePlaceholder: "请描述您的意见" },
    { kind: "other", title: "其他需求", desc: "合规咨询 / 发票 / 更多服务", icon: "他", sla: "服务专员 30 分钟内响应", titlePlaceholder: "请描述您的需求" },
  ],
  memberLevels: {
    专业版: { label: "专业版" },
    标准版: { label: "标准版" },
    游客: { label: "游客" },
  },
  enableTabs: ["chat", "service", "tickets", "messages", "me"],
  supportPhone: "400-800-1234",
};

let current: FrontConfig = DEFAULT_CONFIG;

export function getConfig(): FrontConfig {
  return current;
}

/** 文案占位符插值：{brand} / {agent} */
export function tpl(text: string, cfg: FrontConfig = current): string {
  return text.replaceAll("{brand}", cfg.brandName).replaceAll("{agent}", cfg.agentName);
}

/** 会员等级展示映射（无映射时原样展示） */
export function memberLevelLabel(level: string): string {
  return current.memberLevels[level]?.label ?? level;
}

/* ---------------- 主题色注入 ---------------- */

function hexToRgb(hex: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m || !m[1]) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function mix(a: [number, number, number], b: [number, number, number], t: number): string {
  const c = a.map((v, i) => Math.round(v + ((b[i] ?? 0) - v) * t));
  return `#${c.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

/** 将配置主题色写入 CSS 变量（gold 族 ← primary，holo 族 ← secondary） */
function applyTheme(cfg: FrontConfig): void {
  const root = document.documentElement.style;
  const p = hexToRgb(cfg.theme.primary);
  if (p) {
    root.setProperty("--color-gold", cfg.theme.primary);
    root.setProperty("--color-gold2", mix(p, [0, 0, 0], 0.18));
    root.setProperty("--color-goldhi", mix(p, [255, 255, 255], 0.42));
    root.setProperty("--color-gline", `rgb(${p[0]} ${p[1]} ${p[2]} / 0.45)`);
  }
  const s = hexToRgb(cfg.theme.secondary);
  if (s) {
    root.setProperty("--color-holo", cfg.theme.secondary);
    root.setProperty("--color-holo2", mix(s, [0, 0, 40], 0.25));
  }
}

/** 启动加载：fetch public 配置并深度合并默认值；任何失败都用内置默认 */
export async function loadConfig(): Promise<FrontConfig> {
  try {
    const res = await fetch("service-front.config.json", { cache: "no-cache" });
    if (res.ok) {
      const raw = (await res.json()) as Partial<FrontConfig>;
      current = {
        ...DEFAULT_CONFIG,
        ...raw,
        theme: { ...DEFAULT_CONFIG.theme, ...raw.theme },
        quickReplies: raw.quickReplies?.length ? raw.quickReplies : DEFAULT_CONFIG.quickReplies,
        serviceEntries: raw.serviceEntries?.length ? raw.serviceEntries : DEFAULT_CONFIG.serviceEntries,
        memberLevels: { ...DEFAULT_CONFIG.memberLevels, ...raw.memberLevels },
        enableTabs: raw.enableTabs?.length ? raw.enableTabs : DEFAULT_CONFIG.enableTabs,
      };
    }
  } catch {
    current = DEFAULT_CONFIG;
  }
  applyTheme(current);
  document.title = `${current.brandName} · AI 服务前台`;
  return current;
}
