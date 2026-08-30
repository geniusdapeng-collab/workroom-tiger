#!/usr/bin/env python3
"""围栏生成器（S5 · 单一口径的机器保证）。

从 trading_system/config.py 读取阈值，生成
governance/bundles/trading/fences/trading-baseline.yml：

  - 基线层 R-T1~R-T15（block，不可改）：白皮书红线的机器翻译，
    每条规则的阈值全部由 config 插值，逐条带 "# source:" 注释（config→fence 映射留痕）；
  - 客户 patch 层示例（review，只可加严）；
  - 策略快照层说明（注释段）。

生成记录写 gen_report.json（生成时间 / config 哈希 / 规则条数）。

用法：
  python3 scripts/gen_fences.py            # 生成 YAML + gen_report.json
  python3 scripts/gen_fences.py --check    # 比对现有 YAML 与 config 是否漂移（CI 用，漂移退出码 1）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from trading_system import config  # noqa: E402
from trading_system.redline import STEP_REGISTRY  # noqa: E402

FENCE_PATH = ROOT / "governance/bundles/trading/fences/trading-baseline.yml"
REPORT_PATH = ROOT / "governance/bundles/trading/fences/gen_report.json"
FENCE_VERSION = "trading-baseline/v1"


def _num(x: float) -> str:
    """确定性数值格式化（同 config 同字符串）。"""
    return repr(float(x))


def build_rules() -> list[dict]:
    """从 config 构建规则表。每条规则 (rule_id, name, level, is_baseline,
    match, when, source, note)；when 表达式中的阈值一律来自 config 插值。"""
    ol = config.OPEN_LONG
    lp = config.LIGHT_PROBE
    sr = lp["size_ratio"]
    rules: list[dict] = [
        # ---- 自治层（auto）：夜班/盘前盘后编排动作，模拟盘阶段自治；
        # 实盘阶段由客户 patch 升级为 review（基线单调守卫只可加严）----
        dict(rule_id="R-T0", name="编排自治动作（模拟盘阶段）", level="auto",
             is_baseline=True,
             match={"object_types": ["report", "portfolio"],
                    "actions": ["kernel.doctor", "pipeline.daily",
                                "events.ingest", "site.publish"]},
             when="context.stage == 'paper'",
             source="UPGRADE_PLAN_v3 §8 裁决 R1（模拟盘全 AI 掌控，review 不阻塞）",
             note="夜班 Quest 编排内核管线的自治窗口；stage!=paper 时落 default_level=review"),
        # ---- 基线层（block，不可改）----
        dict(rule_id="R-T1", name="结构/时间止损触及必执行", level="block",
             is_baseline=True,
             match={"object_types": ["position", "order"],
                    "actions": ["position.stop_loss"]},
             when="params.stop_triggered == true && params.executed != true",
             source=f"白皮书§10 止损纪律（时间止损窗口 config.TIME_STOP_BY_ATR={config.TIME_STOP_BY_ATR}）",
             note="止损触及不执行 = 白皮书最大违规；命中即拒绝+P0 告警"),
        dict(rule_id="R-T2", name="单笔风险超 r 上限", level="block",
             is_baseline=True,
             match={"object_types": ["position", "order"],
                    "actions": ["position.open", "order.buy"]},
             when=f"params.risk_pct > {_num(config.RISK_R_PCT)}",
             source=f"config.RISK_R_PCT={_num(config.RISK_R_PCT)}",
             note="1R=账户净值 0.8%，Shares=(Account×r)/|Pin−Psl|"),
        dict(rule_id="R-T3", name="标准开仓三分数联动不满足", level="block",
             is_baseline=True,
             match={"object_types": ["signal", "position"],
                    "actions": ["position.open"]},
             when=(f"params.channel == 'standard' && (params.mrs < {_num(ol['mrs'])}"
                   f" || params.shs < {_num(ol['shs'])}"
                   f" || params.tss < {_num(ol['tss'])})"),
             source=(f"config.OPEN_LONG={{'mrs': {_num(ol['mrs'])}, 'shs': {_num(ol['shs'])},"
                     f" 'tss': {_num(ol['tss'])}}}"),
             note="标准做多 MRS*≥6 且 SHS≥7.5 且 TSS_final≥7.2（白皮书§9.2）"),
        dict(rule_id="R-T4", name="轻仓通道门槛不满足", level="block",
             is_baseline=True,
             match={"object_types": ["signal", "position"],
                    "actions": ["position.open"]},
             when=(f"params.channel == 'light' && (params.mrs < {_num(lp['mrs_lo'])}"
                   f" || params.mrs >= {_num(ol['mrs'])}"
                   f" || params.tss < {_num(lp['tss'])})"),
             source=(f"config.LIGHT_PROBE={{'mrs_lo': {_num(lp['mrs_lo'])}, 'tss': {_num(lp['tss'])}}}"
                     f" + config.OPEN_LONG.mrs={_num(ol['mrs'])}"),
             note="轻仓通道 MRS*∈[5.5,6.0) 且 TSS_final≥7.8"),
        dict(rule_id="R-T5", name="单条产业链累计风险超限", level="block",
             is_baseline=True,
             match={"object_types": ["position", "portfolio"],
                    "actions": ["position.open"]},
             when=f"params.chain_risk_pct > {_num(config.MAX_CHAIN_RISK_PCT)}",
             source=f"config.MAX_CHAIN_RISK_PCT={_num(config.MAX_CHAIN_RISK_PCT)}",
             note="防「N 个独立 R 实为同一个 R」（白皮书§9/§10）"),
        dict(rule_id="R-T6", name="总敞口超 MRS* 仓位上限", level="block",
             is_baseline=True,
             match={"object_types": ["portfolio"],
                    "actions": ["position.open"]},
             when="params.gross_pct > params.mrs_cap",
             source=(f"config.MRS_POSITION_CAP={config.MRS_POSITION_CAP}"
                     f" + config.ENFORCE_GROSS_CAP={config.ENFORCE_GROSS_CAP}"),
             note="mrs_cap 由运行时 MRS* 档位表给出；截断多余 picks"),
        dict(rule_id="R-T7", name="CN T+1 当日回转", level="block",
             is_baseline=True,
             match={"object_types": ["order"],
                    "actions": ["order.sell"]},
             when="context.market == 'cn' && params.same_day_buy == true",
             source="交易所规则（markets/cn 规则包；无 config 数值参数）",
             note="A股当日买入不得当日卖出"),
        dict(rule_id="R-T8", name="涨跌停/VCM 冷静期追单", level="block",
             is_baseline=True,
             match={"object_types": ["order"],
                    "actions": ["order.buy"]},
             when=("(context.market == 'cn' && abs(params.change_pct) >= params.limit_pct)"
                   " || (context.market == 'hk' && params.vcm_cooling == true)"),
             source=(f"config.CN_LIMIT_MAIN={_num(config.CN_LIMIT_MAIN)}"
                     f" / config.CN_LIMIT_STAR_CHINEXT={_num(config.CN_LIMIT_STAR_CHINEXT)}"
                     f" / config.CN_LIMIT_ST={_num(config.CN_LIMIT_ST)}"
                     f" + config.HK_VCM_SYMBOLS（{len(config.HK_VCM_SYMBOLS)} 只）"),
             note="涨跌停追单与 VCM 冷静期买入一律拒绝"),
        dict(rule_id="R-T9", name="单票仓位超 20% 净值", level="block",
             is_baseline=True,
             match={"object_types": ["position"],
                    "actions": ["position.open", "position.adjust"]},
             when=f"after.position_pct > {_num(config.MAX_SINGLE_POSITION_PCT)}",
             source=f"config.MAX_SINGLE_POSITION_PCT={_num(config.MAX_SINGLE_POSITION_PCT)}",
             note="行为风控硬上限（白皮书§10）"),
        dict(rule_id="R-T10", name="MRS*<4 开新仓", level="block",
             is_baseline=True,
             match={"object_types": ["signal", "position"],
                    "actions": ["position.open"]},
             when=f"params.mrs < {_num(config.MRS_GATE_BLOCK)}",
             source=f"config.MRS_GATE_BLOCK={_num(config.MRS_GATE_BLOCK)}",
             note="MRS*<4.0 禁止新开波段仓（除对冲白名单）——刻意不赚的钱"),
        dict(rule_id="R-T11", name="MRS*<6 未按轻仓通道放行", level="block",
             is_baseline=True,
             match={"object_types": ["signal", "position"],
                    "actions": ["position.open"]},
             when=f"params.mrs < {_num(config.MRS_GATE_LIGHT)} && params.channel == 'standard'",
             source=f"config.MRS_GATE_LIGHT={_num(config.MRS_GATE_LIGHT)}",
             note="MRS*<6.0 只许轻仓试探，按标准仓放行即越线"),
        dict(rule_id="R-T12", name="轻仓通道仓位未×0.30–0.40", level="block",
             is_baseline=True,
             match={"object_types": ["position"],
                    "actions": ["position.open"]},
             when=(f"params.channel == 'light' && (params.size_ratio < {_num(sr[0])}"
                   f" || params.size_ratio > {_num(sr[1])})"),
             source=f"config.LIGHT_PROBE.size_ratio={tuple(sr)}",
             note="轻仓试错仓位系数区间（取中值执行）"),
        dict(rule_id="R-T13", name="浮盈≥2R 未做盈利保护", level="block",
             is_baseline=True,
             match={"object_types": ["position"],
                    "actions": ["position.hold", "position.adjust"]},
             when=f"params.profit_r >= {_num(config.PROFIT_PROTECT_R)} && params.protect_active != true",
             source=f"config.PROFIT_PROTECT_R={_num(config.PROFIT_PROTECT_R)}",
             note="浮盈 ≥2R 启动盈利保护（止损上移 +0.5R，白皮书§10）"),
        dict(rule_id="R-T14", name="数据硬依赖全断仍产出报告", level="block",
             is_baseline=True,
             match={"object_types": ["report"],
                    "actions": ["report.emit"]},
             when="context.data_all_down == true",
             source="白皮书§12 诚实失败纪律（新鲜度闸门；无 config 数值参数）",
             note="地基不牢评分皆是沙上之塔；带病产出 = 数据造假"),
        dict(rule_id="R-T15", name="全链路环节缺失", level="block",
             is_baseline=True,
             match={"object_types": ["report"],
                    "actions": ["report.emit"]},
             when="context.steps_missing > 0",
             source=f"redline.STEP_REGISTRY（{len(STEP_REGISTRY)} 环节注册表）",
             note="环节点名缺一即系统性事故（红线 1）"),
        # ---- 客户 patch 层（review 可改，只许收紧；示例）----
        dict(rule_id="R-P1", name="参数变更必审（WFA 提案）", level="review",
             is_baseline=False,
             match={"object_types": ["report"],
                    "actions": ["param.change"]},
             when="true",
             source="白皮书§14 迭代诚实纪律（D7：不显著保持默认）",
             note="策略优化师只提案不生效；DSR 不显著自动驳回，生效次日披露"),
        dict(rule_id="R-P2", name="单票上限收紧示例（20%→15%）", level="review",
             is_baseline=False,
             match={"object_types": ["position"],
                    "actions": ["position.open", "position.adjust"]},
             when=f"after.position_pct > {_num(config.MAX_SINGLE_POSITION_PCT - 0.05)}",
             source=(f"config.MAX_SINGLE_POSITION_PCT={_num(config.MAX_SINGLE_POSITION_PCT)}"
                     "（示例收紧 -5pp，单调守卫：只可加严）"),
             note="patch 层示例：客户可在基线之下收紧，不可放宽"),
        dict(rule_id="R-P3", name="每日新开仓笔数上限示例", level="review",
             is_baseline=False,
             match={"object_types": ["order"],
                    "actions": ["position.open"]},
             when="params.daily_new_opens > 5",
             source="审批纪律示例（无 config 对应参数；patch 层自定，只可加严）",
             note="防过度交易的行为围栏示例"),
    ]
    return rules


def config_hash(rules: list[dict]) -> str:
    """config→fence 映射的指纹：规则表（含插值后表达式）的 sha256。
    config 阈值任何改动都会改变该哈希 → --check 可检出漂移。"""
    payload = json.dumps(rules, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_yaml(rules: list[dict], cfg_hash: str, generated_at: str) -> str:
    """渲染围栏 YAML（确定性：同 rules 同文本；generated_at 仅在头部注释）。"""
    lines: list[str] = [
        "# 老虎交易基线围栏包（S5 · UPGRADE_PLAN_v3 §3.3 三层结构）",
        "# 本文件由 scripts/gen_fences.py 从 trading_system/config.py 生成——生成器产物为唯一发布物，",
        "# 禁止手改（内核零改写红线：阈值的 single source of truth 是 config.py，此处不得有第二套阈值）。",
        "# 单调守卫（F2.3）：is_baseline=true 的规则，任何上层 patch 只可收紧不可放宽。",
        "# DSL 求值语义（对齐 hotel 围栏）：match 命中后求值 when 表达式（沙箱，禁任意代码）；",
        "#   命中 → 按 level 判定；block 优先于 review 优先于 auto（deny 优先并集求值）；",
        "#   写类动作无任何规则命中 → 按 default_level 处理；求值异常 → 按 block（宁可错杀）。",
        f"# 生成记录：config 哈希 {cfg_hash} · 基线规则 15 条 · 生成时间 {generated_at}",
        f"version: {FENCE_VERSION}",
        "default_level: review",
        "rules:",
        "  # ========== 第一层 · 基线层（block，不可改）：白皮书红线的机器翻译 ==========",
    ]
    for r in rules:
        if r["rule_id"] == "R-P1":
            lines += [
                "",
                "  # ========== 第二层 · 客户 patch 层（review 可改，只许收紧；以下为示例）==========",
            ]
        lines += [
            f"  # source: {r['source']}",
            f"  - rule_id: {r['rule_id']}",
            f"    name: {r['name']}",
            f"    level: {r['level']}",
            f"    is_baseline: {'true' if r['is_baseline'] else 'false'}",
            "    match:",
            f"      object_types: [{', '.join(r['match']['object_types'])}]",
            f"      actions: [{', '.join(r['match']['actions'])}]",
            f'    when: "{r["when"]}"',
            f"    note: {r['note']}",
            "",
        ]
    lines += [
        "  # ========== 第三层 · 策略快照层（说明，非规则）==========",
        "  # 每张交易卡片的 Pin/Psl/分批/时间止损快照（白皮书附录C 模板），随信号审批绑定，",
        "  # 盘中触发器严格执行；快照随五元事件留痕（decision.before/after），",
        "  # 盘中任何偏离快照的动作由 R-T1/R-T13 等基线规则接管判定。",
        "",
    ]
    return "\n".join(lines)


def _semantic(yaml_text: str) -> dict:
    """语义化解析（--check 比对口径：忽略注释与时间戳，只比规则内容）。"""
    doc = yaml.safe_load(yaml_text)
    return {"version": doc["version"], "default_level": doc["default_level"],
            "rules": doc["rules"]}


def check() -> int:
    """比对现有 YAML 与 config 是否漂移。0 = 一致；1 = 漂移/缺失。"""
    if not FENCE_PATH.exists():
        print(f"✗ 围栏文件不存在: {FENCE_PATH}（请先运行生成器）")
        return 1
    rules = build_rules()
    expect = _semantic(render_yaml(rules, "<hash>", "<ts>"))
    actual = _semantic(FENCE_PATH.read_text(encoding="utf-8"))
    if actual == expect:
        n = len([r for r in rules if r["is_baseline"]])
        print(f"✓ 围栏与 config 无漂移：{n} 条基线规则（block）+ "
              f"{len(rules) - n} 条 patch 示例逐条一致")
        return 0
    # 定位漂移点
    a_rules = {r["rule_id"]: r for r in actual.get("rules", [])}
    e_rules = {r["rule_id"]: r for r in expect.get("rules", [])}
    bad = 0
    for rid in sorted(set(a_rules) | set(e_rules)):
        if a_rules.get(rid) != e_rules.get(rid):
            bad += 1
            print(f"✗ 漂移: {rid}\n  YAML: {a_rules.get(rid)}\n  config 生成: {e_rules.get(rid)}")
    if actual.get("version") != expect.get("version") or \
       actual.get("default_level") != expect.get("default_level"):
        bad += 1
        print("✗ 漂移: version/default_level 不一致")
    print(f"✗ 围栏与 config 漂移：{bad} 处（config 是 single source of truth，"
          "请运行 scripts/gen_fences.py 重新生成）")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="从 config.py 生成老虎交易围栏包")
    ap.add_argument("--check", action="store_true", help="漂移检测（CI 用）")
    args = ap.parse_args()
    if args.check:
        return check()

    rules = build_rules()
    cfg_hash = config_hash(rules)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = render_yaml(rules, cfg_hash, ts)
    FENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FENCE_PATH.write_text(text, encoding="utf-8")
    baseline_n = len([r for r in rules if r["is_baseline"]])
    report = {
        "generated_at": ts,
        "generator": "scripts/gen_fences.py",
        "config_file": "trading_system/config.py",
        "config_hash": cfg_hash,
        "rules_total": len(rules),
        "baseline_rules": baseline_n,
        "patch_examples": len(rules) - baseline_n,
        "output": str(FENCE_PATH.relative_to(ROOT)),
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(f"✓ 已生成 {FENCE_PATH.relative_to(ROOT)}（基线 {baseline_n} 条 + "
          f"patch 示例 {len(rules) - baseline_n} 条，config 哈希 {cfg_hash[:12]}…）")
    print(f"✓ 生成记录 {REPORT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
