#!/usr/bin/env python3
"""老虎交易 · 多市场官网生成器（v3.4，组合层 + 三市页签）。

读取三市最新运行产物（result_*.json + sim_portfolio.json），生成：
  site/index.html  组合总览（默认首页：三市状态卡 + 组合净值与配置 + 统一风险预算）
  site/us.html / site/cn.html / site/hk.html  各市场完整日报（原样分发）

纪律：
  - 零外部依赖（单文件 + 相对路径，无 CDN/外链）；
  - 缺数据的市场如实标注"当日无有效数据"，绝不编造（D2 诚实失败延伸到组合层）；
  - 组合层只做预算与披露，不做分数与预测（docs/MULTI_MARKET.md §三）。

用法：
  python3 scripts/build_site.py [--us reports] [--cn reports/cn] [--hk reports/hk] [--out site]
"""

from __future__ import annotations

import argparse
import glob
import json
import shutil
from datetime import datetime
from html import escape
from pathlib import Path

MARKETS = [
    {"id": "us", "name": "美股", "full": "美国（NYSE/NASDAQ）", "ccy": "USD",
     "note": "T+1｜无个股涨跌停（LULD）"},
    {"id": "cn", "name": "A股", "full": "中国（沪深）", "ccy": "CNY",
     "note": "T+1｜主板±10%/创科±20%/ST±5%"},
    {"id": "hk", "name": "港股", "full": "中国香港（HKEX）", "ccy": "HKD",
     "note": "T+0｜无涨跌停（VCM 冷静期）"},
]

ACTION_COLOR = {"BUY": "#16a34a", "LIGHT": "#65a30d", "HOLD": "#ca8a04",
                "WAIT": "#6b7280", "AVOID": "#dc2626"}


def _latest(pattern: str) -> str | None:
    fs = sorted(glob.glob(pattern))
    return fs[-1] if fs else None


def load_market(mid: str, out_dir: str) -> dict:
    """读取一个市场的最新产物。缺失时返回 None 字段（如实披露，不编造）。"""
    d = Path(out_dir)
    info: dict = {"id": mid, "ok": False}
    rj = _latest(str(d / "result_*.json"))
    if rj:
        try:
            r = json.loads(Path(rj).read_text())
            mrs = (r.get("mrs") or {})
            info.update(ok=True, date=r.get("trade_date", ""),
                        action=r.get("action", ""),
                        mrs_star=mrs.get("mrs_star"),
                        cap=mrs.get("position_cap"),
                        picks=len(r.get("picks") or []),
                        coverage=(r.get("raw") or {}).get("data_coverage_note")
                        or (r.get("raw") or {}).get("coverage_note") or "")
        except Exception as e:
            info["error"] = str(e)[:120]
    sim = d / "sim_portfolio.json"
    if sim.exists():
        try:
            s = json.loads(sim.read_text())
            curve = s.get("equity_curve") or []
            info["equity"] = curve[-1]["equity"] if curve else s.get("cash")
            info["positions"] = len(s.get("positions") or [])
            info["pending"] = len(s.get("pending") or [])
        except Exception:
            pass
    html_src = _latest(str(d / "日报_*.html"))
    if html_src:
        info["html_src"] = html_src
    return info


def _card(m: dict, meta: dict) -> str:
    if not m.get("ok"):
        return f"""<div class="card dim"><h3>{meta['name']} <span class="sub">{meta['full']}</span></h3>
        <div class="na">当日无有效数据（该市场独立诚实失败，不影响其他市场）</div>
        <div class="sub">{escape(str(m.get('error', '未运行'))[:80])}</div></div>"""
    color = ACTION_COLOR.get(m.get("action", ""), "#6b7280")
    cap = m.get("cap")
    cap_s = f"{cap[0]:.0%} – {cap[1]:.0%}" if isinstance(cap, (list, tuple)) and len(cap) == 2 else "—"
    mrs = m.get("mrs_star")
    mrs_s = f"{mrs:.2f}" if isinstance(mrs, (int, float)) else "—"
    eq = m.get("equity")
    eq_s = f"{meta['ccy']} {eq:,.0f}" if isinstance(eq, (int, float)) else "—"
    return f"""<div class="card"><h3>{meta['name']} <span class="sub">{meta['full']}</span></h3>
    <div class="kv"><span>MRS*</span><b>{mrs_s}</b></div>
    <div class="kv"><span>五态行动</span><b style="color:{color}">{escape(m.get('action', '—'))}</b></div>
    <div class="kv"><span>仓位上限</span><b>{cap_s}</b></div>
    <div class="kv"><span>放行标的</span><b>{m.get('picks', 0)} 只</b></div>
    <div class="kv"><span>模拟盘净值</span><b>{eq_s}</b></div>
    <div class="kv"><span>持仓/待成交</span><b>{m.get('positions', 0)} / {m.get('pending', 0)}</b></div>
    <div class="sub" style="margin-top:6px">{meta['note']}</div>
    <a class="go" href="{meta['id']}.html">进入{meta['name']}完整日报 →</a></div>"""


def render(market_infos: dict[str, dict], out_index: Path) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cards = "".join(_card(market_infos[m["id"]], m) for m in MARKETS)
    eqs = [(m, market_infos[m["id"]].get("equity")) for m in MARKETS]
    eqs = [(m, e) for m, e in eqs if isinstance(e, (int, float))]
    total = sum(e for _, e in eqs) if eqs else None
    bars = ""
    if total and total > 0:
        colors = {"us": "#d4af37", "cn": "#dc2626", "hk": "#2563eb"}
        segs = "".join(
            f'<div style="width:{e / total:.1%};background:{colors[m["id"]]}" '
            f'title="{m["name"]} {e / total:.1%}"></div>' for m, e in eqs)
        legend = " · ".join(f'{m["name"]} {e / total:.1%}' for m, e in eqs)
        bars = f'<div class="bar">{segs}</div><div class="sub">配置占比：{legend}（各市场独立账户净值占比，非风险加权）</div>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>老虎交易 · 组合总览（美/A/港 三市）</title>
<style>
body{{margin:0;background:#0e0f0c;color:#e8e6d8;font:14px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:32px 20px}}
h1{{font-size:26px;margin:0}}h1 .t{{color:#d4af37}}
.sub{{color:#9a988c;font-size:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin:22px 0}}
.card{{background:#171812;border:1px solid #2a2b20;border-radius:14px;padding:18px 20px}}
.card.dim{{opacity:.65}}
.card h3{{margin:0 0 10px;font-size:17px;color:#d4af37}}
.kv{{display:flex;justify-content:space-between;border-bottom:1px dashed #2a2b20;padding:4px 0}}
.kv span{{color:#9a988c}}
.na{{color:#dc2626;font-weight:600;margin:10px 0}}
.go{{display:inline-block;margin-top:10px;color:#d4af37;text-decoration:none;font-weight:600}}
.total{{background:#171812;border:1px solid #d4af3755;border-radius:14px;padding:18px 20px;margin:10px 0 22px}}
.total .num{{font-size:30px;color:#d4af37;font-weight:800}}
.bar{{display:flex;height:14px;border-radius:7px;overflow:hidden;margin:10px 0 4px}}
.bar div{{height:100%}}
.note{{background:#171812;border-left:3px solid #d4af37;border-radius:8px;padding:14px 18px;margin-top:18px}}
.tabs{{display:flex;gap:10px;margin:18px 0 0;flex-wrap:wrap}}
.tabs a{{color:#e8e6d8;background:#1e1f16;border:1px solid #2a2b20;border-radius:999px;padding:6px 16px;text-decoration:none}}
.tabs a.on{{background:#d4af37;color:#171812;font-weight:700}}
.footer{{margin-top:28px;color:#6f6e63;font-size:12px;border-top:1px solid #2a2b20;padding-top:12px}}
</style></head><body><div class="wrap">
<div class="eyebrow sub">TIGER TRADING · MULTI-MARKET PORTFOLIO · EVIDENCE, NOT OPINIONS.</div>
<h1>老虎交易 · <span class="t">组合总览</span> <span class="sub">v3.4 · {now}</span></h1>
<div class="sub">美股 · A股 · 港股 三市统一视图 ｜ 组合层只做预算与披露，不做分数与预测（docs/MULTI_MARKET.md）</div>
<div class="tabs"><a class="on" href="index.html">组合总览</a><a href="us.html">美股</a><a href="cn.html">A股</a><a href="hk.html">港股</a></div>

<div class="grid">{cards}</div>

<div class="total">
  <div class="sub">组合模拟盘净值（三市合计，各市场独立账户简单加总）</div>
  <div class="num">{('≈ ' + format(total, ',.0f')) if total else '—'}</div>
  {bars}
  <div class="sub" style="margin-top:8px">统一风险预算（v1 口径）：组合 Gross Cap ≤ 90%（客户 patch 只可加严）；
  三市共振日按各市场 TOS 排序从高到低截断放行；验证期市场轻仓额度独立计算。</div>
</div>

<div class="note"><b>跨市场注记（披露口径）</b><br>
· 三市低相关持仓本身是自然对冲——组合回撤通常小于任一单市场回撤；对冲价值的量化归因在 M3 阶段落地。<br>
· 货币：HKD 联系汇率锚定 USD，CNY 为真实变量——净值以各市场本地币列示，汇兑波动暂不做对冲建议。<br>
· 主动对冲（指数 ETF/期货）与中央簿记权重调节为设计储备（M4），当前组合层不产生任何跨市场交易动作。<br>
· 各市场独立诚实失败：单市场数据全断只影响本市场页签，组合总览如实标注，绝不编造。</div>

<div class="footer">老虎交易系统（Tiger Trading）· 全 AI 掌控模拟盘 · 虚拟资金 · 不构成投资建议 · 赚亏如实，禁止粉饰</div>
</div></body></html>"""
    out_index.write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--us", default="reports")
    ap.add_argument("--cn", default="reports/cn")
    ap.add_argument("--hk", default="reports/hk")
    ap.add_argument("--out", default="site")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    infos = {mid: load_market(mid, getattr(args, mid)) for mid in ("us", "cn", "hk")}

    # 各市场日报原样分发
    for mid in ("us", "cn", "hk"):
        src = infos[mid].get("html_src")
        if src:
            shutil.copyfile(src, out / f"{mid}.html")
        else:
            (out / f"{mid}.html").write_text(
                f"<!DOCTYPE html><meta charset='utf-8'><body style='font-family:sans-serif;padding:40px'>"
                f"<h2>{mid.upper()} 当日无有效日报</h2><p>该市场当日未运行或独立诚实失败（D2），"
                f"详见 <a href='index.html'>组合总览</a>。</p></body>", encoding="utf-8")

    render(infos, out / "index.html")
    for mid in ("us", "cn", "hk"):
        st = "OK" if infos[mid].get("ok") else "无数据"
        print(f"  {mid}: {st}（action={infos[mid].get('action', '—')}）")
    print(f"✓ 多市场官网已生成：{out}/index.html（组合总览）+ us/cn/hk.html")


if __name__ == "__main__":
    main()
