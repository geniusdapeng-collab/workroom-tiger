/**
 * runtime · 工具执行面（F3.9 五级分层：首版走 L3 确定性剧本——生产主路径）
 * 每个工具产出 { result, receipt }；receipt 是「回执位」（L3.6/E3.7）：
 * 关键数字必须来自工具回执，无回执标「未核实」不得宣称完成。
 * 首版工具为确定性演示剧本（真实 PMS/OTA 适配器进 L1/L2 层，触发条件见总纲 §7）。
 */

export interface ToolReceipt {
  synced: boolean;
  snapshot_uri?: string;
  verified_at?: string;
}

export interface ToolResult {
  result: Record<string, unknown>;
  receipt: ToolReceipt;
}

export type ToolFn = (params: Record<string, unknown>) => Promise<ToolResult>;

const ok = (result: Record<string, unknown>): ToolResult => ({
  result,
  receipt: { synced: true, snapshot_uri: `data/snapshots/${Date.now().toString(36)}.png`, verified_at: new Date().toISOString() },
});

/**
 * 架构 K 修复：mock 工具随机返回 synced:false，让 E3.7 回执校验路径在开发阶段就被走到。
 * 通过环境变量 TOOL_UNVERIFIED_RATE 控制比例（默认 0.1 = 10%）；设为 0 关闭。
 * 仅作用于 demo 工具，不影响真实适配器。
 */
const UNVERIFIED_RATE = Number(process.env.TOOL_UNVERIFIED_RATE ?? "0.1");
function maybeUnverified(result: Record<string, unknown>): ToolResult {
  if (Math.random() < UNVERIFIED_RATE) {
    return { result, receipt: { synced: false } }; // 无回执=未核实（E3.7）
  }
  return ok(result);
}

/** 确定性剧本工具表（云栖酒店演示口径；数字与种子剧本一致） */
export const DEMO_TOOLS: Record<string, ToolFn> = {
  "pms.price.read": async (p) => maybeUnverified({ room_type: p.room_type ?? "RT-DLX-KING", current: 458, occ_7d: 0.78 }),
  "pms.price.write": async (p) => maybeUnverified({ room_type: p.room_type, price: p.price, applied: true }),
  "ota.price.write": async (p) => maybeUnverified({ channel: p.channel ?? "美团", price: p.price, applied: true }),
  "competitor.fetch": async () => maybeUnverified({ card: "西湖云舍酒店", price: 472, ts: new Date().toISOString() }),
  "review.list": async () => maybeUnverified({ fresh: [{ id: "RV-66413", rating: 2, channel: "携程", text: "空调异响影响睡眠" }] }),
  "review.reply": async (p) => maybeUnverified({ review_id: p.review_id, published: true }),
  "order.list": async () => maybeUnverified({ count: 37, total: 18234.5 }),
  "order.reconcile": async () => maybeUnverified({ diff: 0, rounds: 3 }),
  "refund.apply": async (p) => maybeUnverified({ order_id: p.order_id, amount: p.amount, refunded: true }),
  "content.draft": async (p) => maybeUnverified({ title: p.title ?? "秋日云栖套餐", draft_id: `CT-${Date.now().toString(36)}` }),
  "content.publish": async (p) => maybeUnverified({ title: p.title, published: true }),
};

/* ================= 老虎交易 · 真实内核工具（L2 确定性适配器层） =================
 * 与 demo 剧本不同：这些工具真实 spawn 交易内核（Python）与运维脚本，
 * 回执位 receipt.synced 由「产物实证」决定——日报/结果 JSON/官网文件实际存在
 * 且新鲜才标 synced:true（E3.7：无实证不得宣称完成）。
 * 内核命令可用环境变量覆盖（验证/演示用）：
 *   TIGER_KERNEL_CMD  默认 "python3 main.py --mode daily --universe extended --top 30 --picks 8 --html --out reports"
 */
import { execFile } from "node:child_process";
import { existsSync, statSync } from "node:fs";
import { promisify } from "node:util";
import { join } from "node:path";

const execFileP = promisify(execFile);
const KERNEL_ROOT = process.env.TIGER_KERNEL_ROOT ?? join(process.cwd(), "..");
const MAX_BUFFER = 16 * 1024 * 1024;

function freshFile(path: string, maxAgeMin = 30): boolean {
  try {
    const st = statSync(path);
    return Date.now() - st.mtimeMs < maxAgeMin * 60_000;
  } catch { return false; }
}

async function sh(cmd: string, args: string[], cwd: string, timeoutMs: number) {
  const { stdout, stderr } = await execFileP(cmd, args, {
    cwd, timeout: timeoutMs, maxBuffer: MAX_BUFFER,
    env: { ...process.env },
  });
  return { stdout: String(stdout), stderr: String(stderr) };
}

const KERNEL_CMD = (process.env.TIGER_KERNEL_CMD
  ?? "python3 main.py --mode daily --universe extended --top 30 --picks 8 --html --out reports").split(" ");

/** 分市场内核命令（env 可覆盖；out 目录分市场隔离，回执按各自目录实证） */
const MARKET_CMDS: Record<string, { cmd: string[]; outDir: string }> = {
  us: { cmd: KERNEL_CMD, outDir: "reports" },
  cn: {
    cmd: (process.env.TIGER_KERNEL_CMD_CN
      ?? "python3 main.py --market cn --mode daily --universe core --top 30 --picks 8 --html --out reports/cn").split(" "),
    outDir: "reports/cn",
  },
  hk: {
    cmd: (process.env.TIGER_KERNEL_CMD_HK
      ?? "python3 main.py --market hk --mode daily --universe core --top 30 --picks 8 --html --out reports/hk").split(" "),
    outDir: "reports/hk",
  },
};

export const TRADING_TOOLS: Record<string, ToolFn> = {
  /** 源可达性自检（doctor.sh 退出码即回执） */
  "kernel.doctor": async () => {
    const r = await sh("bash", ["scripts/doctor.sh"], KERNEL_ROOT, 180_000).catch((e) => ({ stdout: String(e), stderr: "" }));
    const pass = !r.stdout.startsWith("Error") && !r.stdout.includes("存在硬阻断项");
    return {
      result: { pass, tail: r.stdout.split("\n").slice(-4) },
      receipt: { synced: pass, verified_at: new Date().toISOString() },
    };
  },

  /** 内核全链路（daily/premarket/intraday/demo；params.market=us|cn|hk 分市场） */
  "pipeline.daily": async (p) => {
    const market = String(p.market ?? "us");
    const spec = MARKET_CMDS[market] ?? MARKET_CMDS.us!;
    const outDir = String(p.out_dir ?? spec.outDir);
    const r = await sh(spec.cmd[0]!, spec.cmd.slice(1), KERNEL_ROOT, 3_600_000);
    const today = new Date().toISOString().slice(0, 10).replaceAll("-", "");
    const resultJson = join(KERNEL_ROOT, outDir, `result_${today}.json`);
    const reportHtml = join(KERNEL_ROOT, outDir, `日报_${today}.html`);
    const synced = freshFile(resultJson, 24 * 60) && freshFile(reportHtml, 24 * 60);
    return {
      result: { exit_ok: true, result_json: existsSync(resultJson), report_html: existsSync(reportHtml),
                tail: r.stdout.split("\n").slice(-6) },
      receipt: { synced, snapshot_uri: reportHtml, verified_at: new Date().toISOString() },
    };
  },

  /** 内核事件幂等入库（ingest adapter 输出即回执） */
  "events.ingest": async () => {
    const r = await sh("pnpm", ["tsx", "--env-file=.env", "scripts/ingest-tiger-events.ts"],
                       join(KERNEL_ROOT, "governance"), 300_000);
    const m = /新增 (\d+)，幂等跳过 (\d+)，失败 (\d+)/.exec(r.stdout);
    const synced = !!m && m[3] === "0";
    return {
      result: { ingested: m?.[1] ?? "0", deduped: m?.[2] ?? "0", failed: m?.[3] ?? "?" },
      receipt: { synced, verified_at: new Date().toISOString() },
    };
  },

  /** 盘前作战计划（premarket：ENTRY/STOP/PROTECT 触发器生成） */
  "kernel.premarket": async (p) => {
    const market = String(p.market ?? "us");
    const outDir = market === "us" ? "reports" : `reports/${market}`;
    const cmd = (`python3 main.py --market ${market} --mode premarket `
      + `--universe core --top 30 --picks 8 --html --out ${outDir}`).split(" ");
    const r = await sh(cmd[0]!, cmd.slice(1), KERNEL_ROOT, 3_600_000);
    const today = new Date().toISOString().slice(0, 10).replaceAll("-", "");
    const plan = join(KERNEL_ROOT, outDir, `盘前计划_${today}.md`);
    const synced = freshFile(plan, 24 * 60) || r.stdout.includes("盘前");
    return {
      result: { market, plan_exists: existsSync(plan), tail: r.stdout.split("\n").slice(-4) },
      receipt: { synced, snapshot_uri: plan, verified_at: new Date().toISOString() },
    };
  },

  /** 盘中触发器轮询（intraday：ENTRY/STOP/PROTECT 命中即模拟成交） */
  "kernel.intraday": async (p) => {
    const market = String(p.market ?? "us");
    const outDir = market === "us" ? "reports" : `reports/${market}`;
    const cycles = String(p.cycles ?? "3");
    const interval = String(p.interval ?? "60");
    const cmd = (`python3 main.py --market ${market} --mode intraday `
      + `--interval ${interval} --cycles ${cycles} --out ${outDir}`).split(" ");
    const r = await sh(cmd[0]!, cmd.slice(1), KERNEL_ROOT, 3_600_000);
    // 回执：进程正常结束 + 日报新鲜（盘中模式校验日报新鲜度，过期即拒绝运行——v6.1）
    const synced = !r.stdout.includes("拒绝") && !r.stdout.includes("过期日报");
    return {
      result: { market, cycles, hits: (r.stdout.match(/ENTRY|STOP|PROTECT/g) ?? []).length,
                tail: r.stdout.split("\n").slice(-4) },
      receipt: { synced, verified_at: new Date().toISOString() },
    };
  },

  /** 官网发布（site/index.html 新鲜度即回执） */
  "site.publish": async () => {
    const r = await sh("bash", ["scripts/publish_site.sh"], KERNEL_ROOT, 60_000);
    const site = join(KERNEL_ROOT, "site/index.html");
    const synced = freshFile(site, 10) || r.stdout.includes("已发布");
    return {
      result: { published: synced, tail: r.stdout.trim().split("\n")[0] },
      receipt: { synced, snapshot_uri: "site/index.html", verified_at: new Date().toISOString() },
    };
  },
};

export async function executeTool(name: string, params: Record<string, unknown>): Promise<ToolResult> {
  const fn = TRADING_TOOLS[name] ?? DEMO_TOOLS[name];
  if (!fn) throw new Error(`工具 ${name} 未注册（演示面只含 L3 确定性剧本工具 + 老虎交易内核工具）`);
  return fn(params);
}
