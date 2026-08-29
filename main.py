#!/usr/bin/env python3
"""财神爷AI炒股系统（Caishen AI）— 主入口。

运行模式（--mode）：
  daily      日频全流程：扫描 → 三道门+ICS → 风控 → 日报 + 信号落账/结算（默认）
  premarket  盘前：全流程 + 开盘作战计划 + 昨日信号结算
  intraday   盘中：基于最新日报 picks 做价位触发监控（--interval/--cycles）

独立工具：
  --backtest  无未来函数回测（--bt-days 信号日数）
  --tune      滚动 WFA 调参 + DSR 校正 → tuned_params.json（显著才覆盖默认）

示例：
  python main.py --demo                          # 离线演示
  python main.py --universe full                 # 真实全市场（6000+只两级拉取）
  python main.py --mode premarket --universe full
  python main.py --mode intraday --interval 300 --cycles 12
  python main.py --backtest --demo --bt-days 260
  python main.py --tune --demo --bt-days 380
"""

from __future__ import annotations

import argparse
import glob
import logging
import os

from trading_system import config
from trading_system.pipeline import run_pipeline
from trading_system.report import render_markdown, to_json, to_markdown


def _journal_section(stats: dict) -> str:
    if not stats or stats.get("closed", 0) == 0:
        return "## 十一、胜率追踪（信号日记）\n\n尚无已结算信号——账本从今天开始积累，" \
               "每笔放行信号将按出场规则自动结算。\n"
    lines = ["## 十一、胜率追踪（信号日记）", ""]
    lines.append(f"- 已结算 **{stats['closed']}** 笔（持仓中 {stats['open']}）｜ "
                 f"累计 **{stats['total_r']}R**")
    lines.append(f"- 总胜率 **{stats['win_rate']:.1%}** ｜ 期望 **{stats['expectancy_r']}R** ｜ "
                 f"利润因子 {stats['profit_factor']}")
    l20, l50 = stats["last20"], stats["last50"]
    if l20["n"]:
        lines.append(f"- 近 20 笔: 胜率 {l20['win_rate']:.1%}（期望 {l20['avg_r']}R）"
                     + (f" ｜ 近 50 笔: 胜率 {l50['win_rate']:.1%}" if l50["n"] else ""))
    sf = stats.get("settle_failed")
    if sf:
        lines.append(f"- ⚠️ {len(sf)} 个标的行情缺失无法结算（悬挂待下轮）: "
                     f"{', '.join(sf[:8])}{'...' if len(sf) > 8 else ''}")
    bt = stats.get("by_template", {})
    if bt:
        lines.append("- 分模板: " + " ｜ ".join(
            f"模板{k} {v['n']}笔/胜率{v['win_rate']:.0%}/均{v['avg_r']}R"
            for k, v in bt.items() if v["n"]))
    lines.append("")
    return "\n".join(lines)


def _daily(args, provider) -> None:
    from trading_system.journal import Journal
    from trading_system.providers import get_provider
    from trading_system.state import purge_run_state

    # 零基线纪律：每轮从零开始，先清除上一轮残留（搜索缓存/全市场清单缓存），
    # 白名单仅保留 journal.json（会计台账，不进入决策输入）。
    purged = purge_run_state()
    logging.getLogger(__name__).info("零基线清除: %s", purged)

    result = run_pipeline(
        provider_name=provider,
        universe_mode=args.universe,
        universe_file=args.universe_file,
        top_n=args.top,
        max_picks=args.picks,
        account_usd=args.account,
        use_tuned=args.use_tuned,
        market=args.market,
    )
    json_path = to_json(result, args.out)
    md_path = to_markdown(result, args.out)

    # ---- 信号日记：落账 + 结算 + 统计 ----
    j = Journal(os.path.join(args.out, "journal.json"))
    added = j.log_picks(result, args.account)
    settle_provider = get_provider(provider or ("demo" if args.demo else None))
    settled = j.settle(settle_provider)
    stats = j.stats()
    if getattr(j, "last_failed", None):
        stats["settle_failed"] = j.last_failed      # v6.1：日报披露无法结算清单
    if added or settled:
        logging.getLogger(__name__).info("信号日记: 新落账 %d，新结算 %d", added, settled)

    # ---- 小G模拟盘：全AI掌控模拟盘（初始 $100,000，T+1 开盘价成交，无未来函数）----
    # 台账 sim_portfolio.json 为会计账（零基线白名单保留）；行情一律当日实时拉取。
    from trading_system.pipeline import _single_with_fallback
    from trading_system.simulator import Bar, SimEngine
    sim = SimEngine(os.path.join(args.out, "sim_portfolio.json"))
    _bar_cache: dict = {}

    def get_bar(ticker: str):
        if ticker in _bar_cache:
            return _bar_cache[ticker]
        bar = None
        try:
            df = _single_with_fallback(settle_provider, "ohlcv", ticker, days=400)
            if df is not None and len(df):
                row = df.iloc[-1]
                # v6.3：20 日平均成交额（ADV）用于分档滑点（保守摩擦口径）
                adv = float((df["Close"] * df["Volume"]).tail(20).mean()) \
                    if "Volume" in df.columns else 0.0
                bar = Bar(open=float(row["Open"]), high=float(row["High"]),
                          low=float(row["Low"]), close=float(row["Close"]),
                          adv=adv)
        except Exception as exc:
            logging.getLogger(__name__).warning("小G模拟盘取 %s 日K失败（顺延）: %s", ticker, exc)
        _bar_cache[ticker] = bar
        return bar

    sim_out = sim.step(result.trade_date, result, get_bar)
    sim.save()

    sim_payload = {"state": sim.state, "stats": sim.stats()}
    logging.getLogger(__name__).info("小G模拟盘: 净值 $%.0f，操作 %d 条",
                                     sim_out["equity"], len(sim_out["ops"]))

    # ---- S5 治理桥：内核 → 五元事件（治理旁路，失败只记 WARNING 不阻塞）----
    # 事件账 governance_events.jsonl 属会计账白名单（跨轮累计，不进决策输入）。
    from trading_system.governance_bridge import GovernanceBridge
    bridge = None
    try:
        bridge = GovernanceBridge(os.path.join(args.out, "governance_events.jsonl"))
    except Exception as exc:
        logging.getLogger(__name__).warning("治理桥初始化异常（不阻塞内核）: %s", exc)

    # ---- S6 复盘闭环：诸葛团队日度复盘（归因+违规标记+校准状态 → 复盘纪要）----
    # 复盘产物属会计账白名单，只进报告层与治理事件，不进决策输入（D5）。
    # review.daily 为延迟环节：pipeline 内记 deferred，此处实际执行后补账，
    # 补账先于 emit_from_result——治理事件中的 review.daily 状态才是真实的。
    review_path = None
    try:
        from trading_system.review.chief import ReviewChief
        chief = ReviewChief(out_dir=args.out, journal_path=j.path, bridge=bridge)
        review_out = chief.daily(result.trade_date)
        review_path = review_out["memo_path"]
        for step in result.raw.get("redline", []):
            if step["step"] == "review.daily":
                step.update(status="executed", ms=review_out["ms"], note="")
        logging.getLogger(__name__).info(
            "复盘纪要: %s（违规命中 %d 条）", review_path,
            review_out["attribution"]["total_violations"])
    except Exception as exc:
        # 注册表透传约定：纪要缺失→披露未生成原因（不阻塞内核）
        logging.getLogger(__name__).warning("复盘纪要未生成（披露原因）: %s", exc)
        for step in result.raw.get("redline", []):
            if step["step"] == "review.daily":
                step.update(status="passthrough", note=f"纪要未生成: {exc}")

    if bridge is not None:
        try:
            n_events = bridge.emit_from_result(result, sim_out)
            ok, errs = GovernanceBridge.verify_chain(bridge.path)
            logging.getLogger(__name__).info(
                "治理桥: 写入 %d 条五元事件，哈希链校验 %s", n_events,
                "通过" if ok else f"失败 {errs[:2]}")
        except Exception as exc:
            logging.getLogger(__name__).warning("治理桥异常（不阻塞内核）: %s", exc)

    # review.daily 补账（executed/未生成原因）在首次 to_json 之后——重写 JSON
    # 让落盘结果中的环节状态与治理事件一致（账实相符）。
    json_path = to_json(result, args.out)

    # ---- 盘前计划（premarket 模式）----
    plan_path = None
    if args.mode == "premarket":
        from trading_system.triggers import premarket_plan
        plan = premarket_plan(result)
        plan_path = os.path.join(args.out, f"盘前计划_{result.trade_date.replace('-', '')}.md")
        with open(plan_path, "w", encoding="utf-8") as f:
            f.write(plan)
        print(plan)

    # ---- 把胜率章节追加进日报 ----
    with open(md_path, "a", encoding="utf-8") as f:
        f.write("\n" + _journal_section(stats))

    # ---- HTML 报告（自包含单文件：结论/图形/卡片/红线轨迹/胜率）----
    html_path = None
    if args.html:
        from trading_system.report_html import render_html
        import json as _json
        entries = _json.load(open(j.path, encoding="utf-8")) if os.path.exists(j.path) else []
        # 策略验证中心基准线：QQQ + SPY 同期日线（取不到时页面优雅降级）
        bench: dict = {}
        for bmk in ("QQQ", "SPY"):
            try:
                bdf = _single_with_fallback(settle_provider, "ohlcv", bmk, days=400)
                if bdf is not None and len(bdf):
                    bench[bmk] = [[str(d.date()), round(float(c), 2)]
                                  for d, c in zip(bdf.index, bdf["Close"])]
            except Exception as exc:
                logging.getLogger(__name__).warning("基准 %s 日线获取失败（页面降级）: %s", bmk, exc)
        html_path = os.path.join(args.out, f"日报_{result.trade_date.replace('-', '')}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render_html(result, stats, entries, sim=sim_payload, bench=bench))

    print(f"\n报告已生成:\n  {md_path}\n  {json_path}" +
          (f"\n  {html_path}" if html_path else "") +
          (f"\n  {plan_path}" if plan_path else "") +
          (f"\n  {review_path}" if review_path else "") + "\n")
    if not args.quiet:
        print(render_markdown(result))


def _intraday(args, provider) -> None:
    from trading_system.providers import get_provider
    from trading_system.triggers import monitor_loop, watch_from_result

    files = sorted(glob.glob(os.path.join(args.out, "result_*.json")))
    if not files:
        raise SystemExit("未找到日报 result JSON，请先运行 daily 模式生成 picks。")
    latest = files[-1]
    # v6.1：监控对象必须是【新鲜】日报——拿着几天前的入场/止损参考价做盘中
    # 触发，等于用过期的地图开车（数据时效性红线）。
    import re as _re
    from datetime import datetime as _dt
    from trading_system.calendar import prev_trading_day as _ptd
    m = _re.search(r"result_(\d{4})-?(\d{2})-?(\d{2})", os.path.basename(latest))
    if m:
        rpt_date = _dt.strptime("".join(m.groups()), "%Y%m%d").date()
        earliest = _ptd(_dt.now().date())
        if rpt_date < earliest:
            raise SystemExit(
                f"最新日报为 {rpt_date}（最近交易日 {earliest}）——入场/止损参考价已过期，"
                "请先运行 daily 模式生成今日日报再监控（防止用陈旧数据触发交易动作）。")
    only = [t.strip().upper() for t in args.watch.split(",")] if args.watch else None
    watch = watch_from_result(latest, only)
    if not watch:
        raise SystemExit(f"{latest} 中无 picks 可监控。")
    print(f"监控 {len(watch)} 只（{os.path.basename(latest)}），"
          f"间隔 {args.interval}s × {args.cycles} 轮：")
    for w in watch:
        print(f"  {w['ticker']}: 入场 {w['entry']} / 止损 {w['stop']} / 保护 {w['protect']}")
    prov = get_provider(provider or ("demo" if args.demo else None))
    alerts = monitor_loop(watch, prov, interval_s=args.interval,
                          cycles=args.cycles,
                          on_alert=lambda a: print(f"  ⚡ {a['msg']}"))
    print(f"\n监控结束，共 {len(alerts)} 条触发。")


def _backtest(args, provider) -> None:
    from trading_system.backtest import (
        GateParams, collect_day_frames, run_backtest,
    )
    from trading_system.providers import get_provider
    from trading_system.universe import load_universe

    prov = get_provider(provider or ("demo" if args.demo else None))
    universe = load_universe(args.universe, args.universe_file)
    frames, panel, _ = collect_day_frames(
        prov, universe, days=args.bt_days + 200, signal_days=args.bt_days, top_n=args.top)
    res = run_backtest(frames, panel, GateParams())

    lines = [f"# 回测报告 — {res['n_days']} 个信号日（provider={prov.name}）", ""]
    lines.append(f"- 交易 **{res['n_trades']}** 笔 ｜ 胜率 **{res['win_rate']:.1%}** ｜ "
                 f"期望 **{res['expectancy_r']}R** ｜ 利润因子 {res['profit_factor']}")
    lines.append(f"- 累计 {res['total_r']}R ｜ 组合收益 {res['port_total_return']:.2%} ｜ "
                 f"夏普 {res['port_sharpe']} ｜ 最大回撤 {res['port_max_dd']:.2%}")
    lines.append("")
    # v6.0 诚实披露（白皮书§14 "在统计面前保持诚实"）：净口径与已知偏差
    lines.append("## 口径与已知偏差披露")
    lines.append(f"- 交易成本：单边 {config.COST_BPS}bp 已计入（净口径 R 与组合收益）")
    lines.append("- 幸存者偏差：回测使用【当前】股票池回放历史，已退市标的缺失，"
                 "结果方向性偏乐观；")
    lines.append("- 映射时点偏差：产业链归属/板块映射按【当前】定义应用于历史信号日；")
    lines.append("- 复权口径：yahoo/stooq/ifind 均为前复权（v6.0 统一）。")
    lines.append("")
    if res["by_template"]:
        lines.append("| 模板 | 笔数 | 胜率 | 平均R |")
        lines.append("|---|---|---|---|")
        for k, v in res["by_template"].items():
            lines.append(f"| {k} | {v['n']} | {v['win_rate']:.0%} | {v['avg_r']} |")
        lines.append("")
    path = os.path.join(args.out, f"回测_{prov.name}.md")
    os.makedirs(args.out, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    print(f"\n回测报告: {path}")


def _tune(args, provider) -> None:
    from trading_system.backtest import (
        collect_day_frames, run_wfa, save_tuned_params,
    )
    from trading_system.providers import get_provider
    from trading_system.universe import load_universe

    prov = get_provider(provider or ("demo" if args.demo else None))
    universe = load_universe(args.universe, args.universe_file)
    frames, panel, _ = collect_day_frames(
        prov, universe, days=args.bt_days + 200, signal_days=args.bt_days, top_n=args.top)
    wfa = run_wfa(frames, panel)

    lines = ["# WFA 滚动前推调参报告", ""]
    lines.append(f"折数 {wfa.get('n_folds')} ｜ 网格 {wfa.get('grid_size')} 组合 ｜ "
                 f"试验校正 N={wfa.get('n_folds', 0) * wfa.get('grid_size', 0)}")
    oos = wfa.get("oos_aggregate", {})
    lines.append(f"\n## 样本外（OOS）汇总\n")
    lines.append(f"- 交易 {oos.get('trades')} 笔 ｜ 胜率 {oos.get('win_rate', 0):.1%} ｜ "
                 f"期望 {oos.get('expectancy_r')}R ｜ 夏普 {oos.get('sharpe')} ｜ "
                 f"回撤 {oos.get('max_dd', 0):.2%}")
    lines.append(f"- {wfa.get('dsr_note')}")
    lines.append(f"- 推荐: {wfa.get('recommended_note')}")
    lines.append("\n## 各折明细\n")
    lines.append("| 折 | 训练 | 测试 | 样本内参数 | IS SR | OOS胜率 | OOS期望R |")
    lines.append("|---|---|---|---|---|---|---|")
    for f_ in wfa.get("folds", []):
        lines.append(f"| {f_['fold']} | {f_['train']} | {f_['test']} | {f_['is_params']} "
                     f"| {f_['is_sharpe']:.2f} | {f_['oos_win_rate']:.0%} | {f_['oos_expectancy']} |")
    path = os.path.join(args.out, "WFA报告.md")
    os.makedirs(args.out, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    saved = save_tuned_params(wfa)
    print("\n".join(lines))
    print(f"\nWFA 报告: {path}" + (f"\n调优参数已写入: {saved}" if saved else ""))


def _review_admin(args) -> None:
    """S6 复盘审批流：--review-list / --review-approve / --review-reject。

    模拟盘阶段审批不阻塞 pipeline 运行（只影响参数提案生效）；
    对齐 WorkLoom 三手势：采纳（approve）/ 驳回（reject，原因必填）。
    """
    import os
    from trading_system.governance_bridge import GovernanceBridge
    from trading_system.review.chief import ReviewChief
    from trading_system.review.monthly import list_proposals

    proposals_dir = os.path.join(args.out, "review_proposals")
    bridge = GovernanceBridge(os.path.join(args.out, "governance_events.jsonl"))
    chief = ReviewChief(out_dir=args.out, proposals_dir=proposals_dir,
                        bridge=bridge)

    if args.review_list:
        props = list_proposals(proposals_dir)
        if not props:
            print("暂无参数提案（月度 WFA 生成；DSR 不显著的提案已自动 reject）。")
            return
        print(f"共 {len(props)} 个提案（{proposals_dir}）：")
        for p in props:
            line = (f"  {p.proposal_id}  [{p.status}]  DSR={p.dsr}  "
                    f"OOS期望={p.oos_expectancy}R  "
                    f"参数={p.grid_result.get('recommended_params')}")
            if p.status == "approved":
                line += f"  生效自 {p.effective_from}"
            if p.reason:
                line += f"\n      原因: {p.reason}"
            print(line)
        pending = [p for p in props if p.status == "pending_review"]
        if pending:
            print(f"\n待审批 {len(pending)} 个：--review-approve <id> 或 "
                  "--review-reject <id> --reason \"...\"")
        return

    if args.review_approve:
        p = chief.approve(args.review_approve,
                          tuned_path=os.path.join(args.out, "tuned_params.json"))
        print(f"提案 {p.proposal_id} 已批准：自 {p.effective_from} 起随 "
              f"--use-tuned 显式启用（默认仍不加载：零基线纪律）。\n"
              f"披露已写入 {os.path.join(args.out, 'tuned_params.json')} "
              f"并记入治理事件；次日运行的日报与复盘纪要将披露本次生效。")
        return

    if args.review_reject:
        if not args.reason or not args.reason.strip():
            raise SystemExit("错误：--review-reject 必须同时提供 "
                             "--reason \"...\"（驳回原因必填，三手势纪律）。")
        p = chief.reject(args.review_reject, args.reason)
        print(f"提案 {p.proposal_id} 已驳回，原因已记录并回流: {p.reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 短线美股交易系统 v4.1")
    parser.add_argument("--mode", choices=["daily", "premarket", "intraday"], default="daily")
    parser.add_argument("--market", choices=["us", "cn", "hk"], default="us",
                        help="目标市场（S4 多市场：框架不变、输入替换；默认 us 与现状一致）")
    parser.add_argument("--provider",
                        choices=["yahoo", "stooq", "tencent", "sina", "eastmoney",
                                 "agentgw", "demo"],
                        default=None)
    parser.add_argument("--demo", action="store_true", help="离线演示模式（合成数据）")
    parser.add_argument("--universe", choices=["core", "extended", "full", "file"],
                        default="extended",
                        help="full=官方全市场清单（纽交所+纳斯达克 6000+只，两级拉取）")
    parser.add_argument("--universe-file", default=None)
    parser.add_argument("--top", type=int, default=config.SCAN_TOP_N, help="扫描精评候选数")
    parser.add_argument("--picks", type=int, default=config.MAX_PICKS_DEFAULT, help="最终放行标的数上限")
    parser.add_argument("--account", type=float, default=100_000, help="账户净值（USD）")
    parser.add_argument("--out", default=config.REPORTS_DIR)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--backtest", action="store_true", help="运行无未来函数回测")
    parser.add_argument("--tune", action="store_true", help="滚动 WFA 调参 + DSR 校正")
    parser.add_argument("--bt-days", type=int, default=260, help="回测信号日数")
    parser.add_argument("--interval", type=int, default=300, help="盘中轮询间隔（秒）")
    parser.add_argument("--cycles", type=int, default=12, help="盘中轮询轮数")
    parser.add_argument("--watch", default=None, help="盘中监控子集（逗号分隔代码）")
    parser.add_argument("--use-tuned", action="store_true",
                        help="显式加载上一轮 WFA 调优参数（默认关闭：零基线纪律，每轮从零开始）")
    parser.add_argument("--html", action="store_true", default=True,
                        help="生成自包含 HTML 日报（默认开）")
    parser.add_argument("--no-html", dest="html", action="store_false",
                        help="关闭 HTML 日报")
    # S6 复盘审批流（三手势；模拟盘阶段不阻塞 pipeline，只影响提案生效）
    parser.add_argument("--review-list", action="store_true",
                        help="列出全部 WFA 参数提案及审批状态")
    parser.add_argument("--review-approve", default=None, metavar="ID",
                        help="批准提案：状态→approved，次日随 --use-tuned 生效并披露")
    parser.add_argument("--review-reject", default=None, metavar="ID",
                        help="驳回提案：必须同时提供 --reason（原因必填）")
    parser.add_argument("--reason", default=None,
                        help="驳回原因（--review-reject 必填，回流组织记忆）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    provider = "demo" if args.demo else args.provider

    if args.review_list or args.review_approve or args.review_reject:
        _review_admin(args)
        return

    if args.backtest or args.tune:
        if args.backtest:
            _backtest(args, provider)
        if args.tune:
            _tune(args, provider)  # 帧缓存复用，WFA 在缓存上跑
    elif args.mode == "intraday":
        _intraday(args, provider)
    else:
        _daily(args, provider)


if __name__ == "__main__":
    main()
