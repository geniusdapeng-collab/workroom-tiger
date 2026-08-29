/**
 * PII 脱敏模块（安全网关三段瀑布·第二段）
 * D7 薄自研替代（Presidio 独立服务进停车场）：中文 PII 正则 + 占位符协议
 * 协议：命中片段替换为 [PII:<KIND>:<sha256 前 8 位>] —— 同值同占位（可关联），无明文落事件
 * 编号回引：F1.10（PII 保险柜）/ 铁律 1（一切事件写入必经脱敏段）
 * 纯函数，零依赖，可单测。
 */
import { createHash } from "node:crypto";

export type PiiKind = "PHONE" | "IDCARD" | "EMAIL" | "BANKCARD" | "QQ";

interface PiiRule {
  kind: PiiKind;
  pattern: RegExp;
  /** 可选二次校验（如银行卡 Luhn），返回 false 则不替换 */
  verify?: (raw: string) => boolean;
}

/** 规则表（只可加严不可删除；命中即替换） */
const RULES: PiiRule[] = [
  // 身份证号（18 位，末位可 X）优先于手机号（避免 11 位子串误伤）
  { kind: "IDCARD", pattern: /\b\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b/g },
  // 银行卡（13–19 位连续数字，排除已命中的身份证位；Luhn 二次确认避免误伤订单号/时间戳）
  { kind: "BANKCARD", pattern: /\b(?:\d[ -]?){13,19}\b/g, verify: luhnValid },
  // 大陆手机号
  { kind: "PHONE", pattern: /\b1[3-9]\d{9}\b/g },
  // 邮箱
  { kind: "EMAIL", pattern: /\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b/g },
  // QQ 号（5–11 位，带「QQ」语境才命中，避免误伤纯数字价格/订单号）
  { kind: "QQ", pattern: /(?<=[QqＱｑ]{2}[:：\s]?)\d{5,11}\b/g },
];

/** Luhn 校验（银行卡标准校验算法，过滤订单号/时间戳等非卡数字） */
function luhnValid(raw: string): boolean {
  const digits = raw.replace(/[\s-]/g, "");
  if (digits.length < 13 || digits.length > 19) return false;
  let sum = 0;
  let alt = false;
  for (let i = digits.length - 1; i >= 0; i--) {
    const n = Number(digits[i]);
    if (!Number.isInteger(n)) return false;
    if (alt) {
      const double = n * 2;
      sum += double > 9 ? double - 9 : double;
    } else {
      sum += n;
    }
    alt = !alt;
  }
  return sum % 10 === 0;
}

function placeholder(kind: PiiKind, raw: string): string {
  const digest = createHash("sha256").update(raw, "utf-8").digest("hex").slice(0, 8);
  return `[PII:${kind}:${digest}]`;
}

/** 对任意字符串脱敏；返回脱敏后文本与命中计数 */
export function maskText(input: string): { text: string; hits: number } {
  let text = input;
  let hits = 0;
  for (const { kind, pattern, verify } of RULES) {
    text = text.replace(pattern, (raw) => {
      if (verify && !verify(raw)) return raw; // 二次校验不过则保留原文
      hits += 1;
      return placeholder(kind, raw);
    });
  }
  return { text, hits };
}

/**
 * 对事件 payload 递归脱敏（字符串叶子全部过规则；键名不动）。
 * 注意：pii_vault 原文映射的写入在阶段二后续卡（profiles 表）落地，本段只保证事件无明文。
 */
export function maskDeep<T>(value: T): { value: T; hits: number } {
  let hits = 0;
  const walk = (v: unknown): unknown => {
    if (typeof v === "string") {
      const r = maskText(v);
      hits += r.hits;
      return r.text;
    }
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") {
      return Object.fromEntries(Object.entries(v as Record<string, unknown>).map(([k, x]) => [k, walk(x)]));
    }
    return v;
  };
  return { value: walk(value) as T, hits };
}
