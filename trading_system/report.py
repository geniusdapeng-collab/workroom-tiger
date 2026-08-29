"""报告生成 — Markdown 日报（四行看板 + 产业链周期章节 + 交易卡片）。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from . import config
from .data_models import PipelineResult


def to_json(result: PipelineResult, out_dir: str = config.REPORTS_DIR) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    ts = result.trade_date.replace("-", "")
    path = Path(out_dir) / f"result_{ts}.json"
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    return str(path)


def to_markdown(result: PipelineResult, out_dir: str = config.REPORTS_DIR) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    ts = result.trade_date.replace("-", "")
    path = Path(out_dir) / f"日报_{ts}.md"
    path.write_text(render_markdown(result), encoding="utf-8")
    return str(path)


def render_markdown(r: PipelineResult) -> str:
    m = r.mrs
    lines: list[str] = []
    lines.append(f"# AI 短线美股交易日报 — {r.trade_date}")
    lines.append("")
    lines.append(f"> 数据源: {r.provider} ｜ 股票池: {r.raw.get('universe_mode')} "
                 f"（覆盖 {r.raw.get('data_coverage')}）｜ 耗时 {r.raw.get('elapsed_s')}s")
    lines.append("")

    # ---- 老板版四行看板（白皮书 §2.7）----
    lines.append("## 一、四行看板")
    lines.append("")
    if m:
        main_sectors = [s for s in r.sectors if s.in_main_pool]
        sub_sectors = [s for s in r.sectors if s.in_sub_pool]
        main_txt = "；".join(f"{s.etf}(SHS {s.shs})" for s in main_sectors) or "无"
        lines.append(f"1. **市场状态（MRS*）**：{m.mrs_star}/10（{m.regime}，k={m.k}）")
        lines.append(f"2. **总仓位上限**：{m.position_cap[0]:.0%} - {m.position_cap[1]:.0%}")
        lines.append(f"3. **主线板块**：{main_txt}" +
                     (f" ｜ 次主线: " + "；".join(f"{s.etf}(SHS {s.shs})" for s in sub_sectors) if sub_sectors else ""))
        lines.append(f"4. **允许交易标的**：{len(r.picks)} 只 → " +
                     ("、".join(p.ticker for p in r.picks) if r.picks else "无达标标的，等待"))
    lines.append("")
    lines.append(f"**系统指令**：`{r.action}` — {r.market_view}")
    lines.append("")

    # ---- MRS 明细 ----
    if m:
        lines.append("## 二、MRS 市场共振（第一道门）")
        lines.append("")
        lines.append("| 维度 | 得分 | 明细 |")
        lines.append("|---|---|---|")
        for key, d in m.dimensions.items():
            detail = d.evidence[0] if d.evidence else ""
            miss = f"（缺失: {', '.join(d.missing)}）" if d.missing else ""
            score_txt = d.score if d.score is not None else "缺失·已剔除"
            lines.append(f"| {key} | {score_txt} | {detail}{miss} |")
        lines.append("")
        lines.append(f"MRS_raw={m.mrs_raw} → Δ={m.delta}（五维极差）→ k={m.k} → **MRS*={m.mrs_star}**")
        lines.append("")

    # ---- SHS ----
    lines.append("## 三、SHS 板块热度（第二道门）")
    lines.append("")
    lines.append("| 板块ETF | SHS | 宏观 | 资金动量 | 叙事 | 微观 | 广度 | 池 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in r.sectors:
        pool = "主线" if s.in_main_pool else "次主线" if s.in_sub_pool else ""
        breadth = f"{s.breadth:.0f}%" if s.breadth == s.breadth else "N/A"
        f_ = lambda k: s.factors.get(k) if s.factors.get(k) is not None else "—"
        lines.append(f"| {s.etf} | {s.shs} | {f_('macro')} | {f_('flow')} "
                     f"| {f_('narr')} | {f_('micro')} | {breadth} | {pool} |")
    lines.append("")

    # ---- 产业链周期（新增模块）----
    lines.append("## 四、产业链周期 ICS（新增维度）")
    lines.append("")
    lines.append("| 产业链 | ICS | 周期阶段 | 领涨环节 | 链内广度 | 轮动信号 | 热区 |")
    lines.append("|---|---|---|---|---|---|---|")
    for c in r.chains:
        breadth = f"{c.breadth:.0f}%" if c.breadth == c.breadth else "N/A"
        lines.append(f"| {c.name} | {c.ics} | {c.stage} | {c.leading_link} | {breadth} "
                     f"| {c.rotation_signal} | {'🔥' if c.hot else ''} |")
    lines.append("")
    lines.append("<details><summary>各链明细证据</summary>")
    lines.append("")
    for c in r.chains:
        for e in c.evidence:
            lines.append(f"- {e}")
    lines.append("")
    lines.append("</details>")
    lines.append("")

    # ---- 扫描统计 ----
    ss = r.raw.get("scan_stats", {})
    if ss:
        lines.append("## 五、全市场扫描（替代硬编码 watchlist）")
        lines.append("")
        lines.append(f"- 股票池: {ss.get('universe_size')} 只 → 通过硬过滤: {ss.get('passed')} 只 → 精评候选: {ss.get('selected')} 只")
        rej = ss.get("rejected", {})
        lines.append(f"- 过滤明细: 低价 {rej.get('price', 0)} ｜ 流动性不足 {rej.get('adv', 0)} ｜ "
                     f"历史不足 {rej.get('history', 0)} ｜ 波动病态 {rej.get('atr', 0)} ｜ 无数据 {rej.get('nodata', 0)}")
        top10 = ss.get("top10", [])
        if top10:
            lines.append(f"- 初排 Top10: " + "，".join(f"{t}({s})" for t, s in top10))
        lines.append("")

    # ---- TSS 精评 ----
    lines.append("## 六、TSS 精评（第三道门）")
    lines.append("")
    lines.append("| 标的 | TSS_final | 结构 | 动能 | 期权 | 模板 | 关键位 | 产业链 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for c in r.watchlist[:15]:
        s_opt = c.s_options if c.s_options is not None else "—"
        lines.append(f"| {c.ticker} | {c.tss_final} | {c.s_structure} | {c.s_momentum} "
                     f"| {s_opt} | {c.entry_template or '-'} | {c.key_level:.2f} | {c.chain_id or '-'} |")
    lines.append("")

    # ---- 交易卡片 ----
    lines.append("## 七、风控放行与交易卡片")
    lines.append("")
    if r.picks:
        for p in r.picks:
            lines.append("```")
            lines.append(p.card)
            lines.append("```")
            lines.append("")
    else:
        lines.append("无放行标的。")
        lines.append("")

    # ---- 备注与缺失披露 ----
    lines.append("## 八、备注与数据缺失披露")
    lines.append("")
    for n in r.notes:
        lines.append(f"- {n}")
    lines.append("- 叙事兑现（EPS 修正/指引）自 v5.0 起由 LLM 驱动（NarrativeAgent）；"
                 "LLM 不可用时按红线透传并剔除再归一化，绝不用规则估算。")
    lines.append("- 微观（GEX/0DTE/Skew）免费源缺失：缺失因子在聚合时【剔除并再归一化】，"
                 "期权维度（PCR/IV 分位）随信号日记逐日积累满 10 个样本后自动启用真实评分。")
    lines.append("")

    # ---- 科技产业链专项子集群（标准化汇入信号）----
    tech = r.raw.get("tech_signals", [])
    if tech:
        lines.append("## 九、科技产业链专项（子集群 → 主决策引擎）")
        lines.append("")
        lines.append(f"- 采集 {r.raw.get('docs_collected', 0)} 篇 → 清洗 {r.raw.get('docs_cleaned', 0)} 篇"
                     f"（语义标注缺失 {r.raw.get('docs_semantic_degraded', 0)} 篇，红线透传）")
        lines.append("")
        lines.append("| 子链 | 景气度 | 风险 | 加成 | 领涨环节 | 传导图 | 降级环节 |")
        lines.append("|---|---|---|---|---|---|---|")
        for s in tech:
            trans = " ".join(f"{a}→{b}({v:.2f})" for a, tg in s.get("transmission", {}).items()
                             for b, v in tg.items()) or "—"
            deg = ",".join(s.get("degraded_components", [])) or "—"
            pros = s.get("prosperity")
            lines.append(f"| {s.get('chain_id')} | {pros if pros is not None else '—'} "
                         f"| {s.get('risk_level', 0):.0f} | ×{s.get('bonus_hint', 1.0)} "
                         f"| {s.get('leading_link') or '—'} | {trans} | {deg} |")
        lines.append("")
        alerts_all = [a for s in tech for a in s.get("alerts", [])]
        if alerts_all:
            lines.append("**风险预警**：")
            for a in alerts_all[:8]:
                lines.append(f"- ⚠️ [{a.get('severity'):.0f}/10·{a.get('type')}] "
                             f"{a.get('headline_zh')}（传导: {'/'.join(a.get('transmission', [])) or '—'}）")
            lines.append("")
        lines.append("<details><summary>各子链证据</summary>")
        lines.append("")
        for s in tech:
            for e in s.get("evidence", []):
                lines.append(f"- [{s.get('chain_id')}] {e}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # ---- 红线执行轨迹（全链路完整性审计）----
    redline = r.raw.get("redline", [])
    if redline:
        lines.append("## 十、红线执行轨迹（环节无一遗漏的机器证明）")
        lines.append("")
        lines.append("| 环节 | 状态 | 耗时ms | 备注 |")
        lines.append("|---|---|---|---|")
        for s in redline:
            mark = "✅" if s["status"] == "executed" else "🔁透传"
            lines.append(f"| {s['step']} | {mark} | {s['ms']} | {s.get('note', '')} |")
        lines.append("")

    lines.append("---")
    lines.append("*非预测、重流程、可审计。本报告不构成投资建议。*")
    return "\n".join(lines)
