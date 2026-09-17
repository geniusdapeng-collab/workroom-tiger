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


def _debate_card_section(ticker: str, debate: dict) -> str:
    """交易卡片"第六段：多空辩论证据"（v6.3 S2）。

    三种状态：有证据 → 渲染证据；触发但 LLM 透传 → 标注"本轮无辩论证据"；
    未触发（灰区外）→ 空串（不渲染）。
    """
    ev = (debate.get("evidence") or {}).get(ticker)
    if ev:
        bull = "；".join(ev.get("bull_points") or []) or "—"
        bear = "；".join(ev.get("bear_points") or []) or "—"
        fals = "；".join(ev.get("falsification_conditions") or []) or "—"
        return (
            f"  多空辩论证据（第六段，仅供复核，不影响评分与闸门）:\n"
            f"    多头论据: {bull}\n"
            f"    空头论据: {bear}\n"
            f"    裁决: {ev.get('coordinator_verdict', '存疑')}"
            f"（{ev.get('verdict_reason', '')}）\n"
            f"    证伪条件: {fals}"
        )
    if ticker in (debate.get("triggered") or []):
        if debate.get("status") == "passthrough":
            return "  多空辩论证据（第六段）: 本轮无辩论证据（LLM 不可用，红线透传兜底）"
        return "  多空辩论证据（第六段）: 本轮无有效辩论产出（已如实记录）"
    return ""


def render_markdown(r: PipelineResult) -> str:
    m = r.mrs
    mk = r.raw.get("market") or {}
    mk_grad = r.raw.get("market_grad") or {}
    lines: list[str] = []
    lines.append(f"# AI 短线{mk.get('short_name', '美股')}交易日报 — {r.trade_date}")
    lines.append("")
    lines.append(f"> 数据源: {r.provider} ｜ 股票池: {r.raw.get('universe_mode')} "
                 f"（覆盖 {r.raw.get('data_coverage')}）｜ 耗时 {r.raw.get('elapsed_s')}s")
    if mk:
        grad_txt = ("标准通道已开放" if mk_grad.get("graduated", True)
                    else f"新市场验证期（{mk_grad.get('settled', 0)}"
                         f"/{mk_grad.get('required', 50)}）·强制轻仓")
        lines.append(f"> 市场: **{mk.get('name')}**（{mk.get('market_id')} ｜ "
                     f"{mk.get('timezone')} ｜ 结算 {mk.get('settlement')} ｜ "
                     f"{mk.get('limit_note')}）｜ 通道状态: {grad_txt}")
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
    debate = r.raw.get("debate", {}) or {}
    if r.picks:
        for p in r.picks:
            lines.append("```")
            lines.append(p.card)
            # v6.3 S2：交易卡片"第六段：多空辩论证据"（只读证据附加，
            # 辩论绝不修改任何分数与闸门输出；LLM 透传时如实标注）
            sec = _debate_card_section(p.ticker, debate)
            if sec:
                lines.append(sec)
            lines.append("```")
            lines.append("")
    else:
        lines.append("无放行标的。")
        lines.append("")
    if debate.get("status") == "passthrough" and debate.get("triggered"):
        lines.append("> 本轮触发多空辩论的标的（"
                     + "、".join(debate["triggered"])
                     + "）因 LLM 不可用走红线透传：本轮无辩论证据。")
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
    lines.append(f"- 多空辩论证据层（v6.3 S2）：仅灰区标的触发（每日上限 "
                 f"{config.DEBATE_MAX_PER_DAY} 场，HOLD 区高分线 "
                 f"{config.DEBATE_HOLD_TSS_MIN}），辩论结论只作交易卡片第六段证据，"
                 "不修改任何分数与闸门输出；回测路径禁用。")
    # ---- S4 多市场披露：市场标识 / 基准缺失 / 板块广度缺失 / 合规校验 / 链覆盖率 ----
    if mk:
        lines.append("")
        lines.append("### 多市场披露（S4：框架不变、输入替换）")
        lines.append("")
        bmk = mk.get("benchmarks", {})
        lines.append(f"- 基准组：指数 {bmk.get('index_label', bmk.get('index'))}"
                     f"（{bmk.get('index')}）｜ 利率 {bmk.get('rate_label', bmk.get('rate'))}"
                     f"（{bmk.get('rate')}）｜ 波动率 {bmk.get('vol_label', bmk.get('vol'))}"
                     f"（{bmk.get('vol')}）")
        fresh = r.raw.get("freshness") or {}
        if fresh.get("benchmark_missing"):
            lines.append(f"- ⚠️ 基准缺失（剔除再归一化，不钉中性分）: "
                         f"{'、'.join(fresh['benchmark_missing'])}")
        if fresh.get("sector_breadth_missing"):
            lines.append(f"- ⚠️ {fresh['sector_breadth_missing']}")
        sf = mk.get("scan_filters", {})
        lines.append(f"- 扫描硬过滤：价格 ≥ {sf.get('min_price')} {sf.get('currency')} ｜ "
                     f"20 日成交额 ≥ {sf.get('min_adv'):,} {sf.get('currency')}")
        lines.append(f"- 产业链映射覆盖率（本市场）: {r.raw.get('chain_coverage', '—')}")
        comp = r.raw.get("compliance") or []
        violations = [c for c in comp if not c.get("allowed")]
        rules = "、".join(mk.get("compliance_rules", []))
        lines.append(f"- 合规规则（围栏前置）: {rules}")
        if violations:
            for v in violations:
                lines.append(f"  - ❌ {v.get('ticker')} {v.get('side')}: "
                             f"{v.get('reason')}（{v.get('rule_id')}）")
        else:
            lines.append(f"  - 本轮校验 {len(comp)} 笔买入请求，全部放行（无违反项）")
        if not mk_grad.get("graduated", True):
            lines.append(f"- D7 轻仓通道：新市场验证期（{mk_grad.get('settled', 0)}"
                         f"/{mk_grad.get('required', 50)}），全部放行标的强制轻仓 "
                         f"×{sum(config.MARKET_LIGHT_SIZE) / 2:.2f}"
                         "（满 50 笔结算且 DSR 过关才开放标准通道，门槛见 "
                         "config.MARKET_GRADUATION）")
    lines.append("")

    # ---- 数据可信度与交叉验证（v6.3 S3 数据层披露：规则层调度，不引入 LLM）----
    cv = r.raw.get("cross_validation") or {}
    if cv:
        lines.append("### 数据可信度与交叉验证（S3 Tiger Data Fabric）")
        lines.append("")
        lines.append(f"- 交叉验证统计：总文档 **{cv.get('total', 0)}** 篇 ｜ "
                     f"关键事件类 {cv.get('key_event_docs', 0)} 篇 ｜ "
                     f"通过（≥2源且至少一个≤T2）**{cv.get('corroborated', 0)}** 篇 ｜ "
                     f"被降级 **{cv.get('downgraded', 0)}** 篇")
        lines.append(f"- 其中 LLM 语义标注缺失记未验证 {cv.get('llm_missing', 0)} 篇（红线透传，不阻塞管线）；"
                     f"缺发布时间（Point-in-Time）{cv.get('missing_published_at', 0)} 篇")
        lines.append("- 口径：来源可信度 T0 监管原文 > T1 主流媒体 > T2 聚合门户 > T3 社媒"
                     "（config.SOURCE_TIERS/DOMAIN_TIERS 可覆盖）；被降级文档不删除，"
                     "决策侧仅作背景参考，叙事/舆情打分只使用 corroborated=True 集合。")
        ah = r.raw.get("ah_topics_enabled", False)
        lines.append(f"- A/H 市场情报主题（S4 铺路）：**{'已启用' if ah else '默认关闭'}**"
                     f"（config.AH_TOPICS_ENABLED={'True' if ah else 'False'}，"
                     "关闭时不产生任何抓取调用）。")
        lines.append("")

    # ---- 统计校准（v6.3 S2 迭代层披露：只进报告，不改任何闸门）----
    cal = r.raw.get("calibration", {}) or {}
    if cal:
        lines.append("### 统计校准（迭代层 · 只进报告不改闸门）")
        lines.append("")
        if cal.get("status") == "skipped":
            lines.append(f"- 本轮校准环节失败，跳过披露并记录：{cal.get('reason', '')}")
        else:
            lines.append(f"- {cal.get('disclosure', '')}")
            lines.append("")
            lines.append("| TSS 分桶 | 样本数 | 实际胜率 | Wilson 95% 区间 | 期望R |")
            lines.append("|---|---|---|---|---|")
            for b in cal.get("buckets", []):
                wr = f"{b['win_rate']:.1%}" if b.get("win_rate") is not None else "—"
                interval = (f"[{b['wilson'][0]:.1%}, {b['wilson'][1]:.1%}]"
                            if b.get("n") else "—")
                ar = b["avg_r"] if b.get("avg_r") is not None else "—"
                lines.append(f"| {b['bucket']} | {b['n']} | {wr} | {interval} | {ar} |")
            if cal.get("monotonic") is not None:
                lines.append("")
                lines.append(f"- 分数-结果单调性：{'✅ 成立' if cal['monotonic'] else '⚠️ 不成立（需复盘）'}"
                             f"（样本 {cal.get('n')} ≥ {cal.get('min_samples')}，输出校准曲线点）")
        lines.append("")
        lines.append(f"> 口径披露：样本门槛 {config.CALIBRATION_MIN_SAMPLES} 条"
                     f"（config.CALIBRATION_MIN_SAMPLES），样本库 "
                     f"{config.CALIBRATION_SAMPLES_PATH} 属会计账白名单，"
                     "与 journal 同级，禁止进入决策输入。")
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
