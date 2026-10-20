/**
 * 运行配置横幅（D24 落地向导入口）
 *
 * 事实源 = onboarding.status；分别展示档案声明、正式启用资格与模型验证。
 * 缺失/非法声明、资格失效及查询失败不能被解释成已切换到模拟执行。
 * 挂载点：P0 经营主页顶栏下方 + Bridge 工作台顶栏下方（全覆盖所有页面）。
 */
import { useEffect, useState } from "react";
import { ensureDemoLogin, trpc } from "../lib/trpc";
import { Icon } from "@workloom/ui";

export interface OnboardingStatus {
  /** @deprecated 历史正式资格标记；不代表数据来源或工具实际执行模式。 */
  dataMode: "simulated" | "real";
  /** @deprecated 历史归一化字段；缺失声明也可能被归为 simulated。 */
  persistedDataMode?: "simulated" | "real";
  declaredDataMode?: "simulated" | "real" | "unconfigured" | "invalid";
  defaultToolMode?: "simulated" | "connector-required";
  formalModeActive?: boolean;
  formalActivationRecorded?: boolean;
  llm: { provider: string; model: string; baseUrl: string; real: boolean };
  workspace: { name: string; events: number; members: number; agents: number; memories: number };
  business?: { name: string; industry: string; note: string; configuredAt: string; source: string } | null;
  bundle?: { id: string | null; isExample: boolean };
  activationGate?: {
    canActivate: boolean;
    blockers: string[];
    checks: Array<{ key: string; label: string; ok: boolean; detail: string }>;
  };
}

/** 新字段缺失时保持未知；旧资格别名只用于资格兼容，不能推导档案声明。 */
export function onboardingModeSummary(status: OnboardingStatus) {
  const dataModeText = status.declaredDataMode === "simulated" ? "已声明模拟模式"
    : status.declaredDataMode === "real" ? "已声明非模拟模式"
      : status.declaredDataMode === "unconfigured" ? "数据模式未配置"
        : status.declaredDataMode === "invalid" ? "数据模式未知（档案声明无效）" : "数据模式未知（服务端未提供声明）";
  const toolModeText = status.defaultToolMode === "simulated" ? "通用工具可返回本地模拟回执"
    : status.defaultToolMode === "connector-required" ? "通用工具需要接入连接器并取得执行回执" : "通用工具模式尚未确认";
  const formalModeActive = status.formalModeActive ?? status.dataMode === "real";
  return { dataModeText, toolModeText, formalModeActive };
}

export function SimBanner() {
  const [st, setSt] = useState<OnboardingStatus | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        await ensureDemoLogin();
        const s = (await trpc.onboarding.status.query()) as OnboardingStatus;
        if (!stop) { setSt(s); setLoadFailed(false); }
      } catch {
        if (!stop) { setSt(null); setLoadFailed(true); }
      }
    };
    void load();
    const id = setInterval(() => void load(), 15_000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, []);
  if (!st) return loadFailed ? (
    <div role="status" className="relative z-10 border-b border-amber-500/50 bg-amber-100/80 px-4 py-2 text-body text-amber-800">
      运行状态暂时无法确认，请稍后重试；具体执行结果以任务回执为准。
    </div>
  ) : null;
  const mode = onboardingModeSummary(st);
  const modelText = st.llm.real ? "当前模型配置已通过验证"
    : st.llm.provider === "mock" ? "当前使用内置模拟模型" : "当前模型配置尚未通过验证";
  const modeText = <>{mode.dataModeText}；{mode.formalModeActive ? "正式启用资格已确认" : "未正式启用"}。
    {mode.toolModeText}；{modelText}。具体数据来源与执行结果以任务回执为准。</>;
  // V4 §2：示例版银带（深空银辉语义——不是警告，是身份说明；与黄色模拟态警示分色）
  if (st.bundle?.isExample) {
    return (
      <div className="relative z-10 flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line bg-bg800/90 px-4 py-1.5 text-body text-ink2 backdrop-blur">
        <Icon name="star" size={15} className="text-gold" />
        <span className="min-w-0 flex-1 break-words">
          当前装配：<b className="text-ink">行业示例版</b>（{st.workspace.name}）。{modeText}
          可按引导定制您的专属行业版。
        </span>
        <a
          href="/onboarding?mode=customize"
          className="shrink-0 rounded border border-gline bg-bg700 px-3 py-1 font-bold text-ink no-underline transition-colors hover:bg-bg700"
        >
          定制我的行业版 →
        </a>
      </div>
    );
  }
  if (st.declaredDataMode === "real" && mode.formalModeActive && st.llm.real) return null;
  return (
    <div className="relative z-10 flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-amber-500/50 bg-amber-100/80 px-4 py-2 text-body text-amber-800 backdrop-blur">
      <Icon name="warning" size={16} />
      <span className="min-w-0 flex-1 break-words">
        {modeText} 请进入「落地向导」查看并补齐配置。
      </span>
      <a
        href="/onboarding"
        className="shrink-0 rounded border border-amber-500/60 bg-amber-200/60 px-3 py-1 font-bold text-amber-900 no-underline transition-colors hover:bg-amber-300/60"
      >
        查看落地向导 →
      </a>
    </div>
  );
}
