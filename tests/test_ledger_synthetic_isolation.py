"""合成数据/测试数据不得进入产品台账（v6.5 排雷回归锁定）。

覆盖四个缺陷（详见 outputs/mine-clear/T-2026-0929-0002/）：
  ① `main.py --demo` 把合成成交写进公开验证台账（sim_portfolio.json / journal.json）
  ② 测试夹具（DemoProvider 子类改名）绕过 `name == "demo"` 守卫，写进
     reports/options_hist 真实期权分位历史
  ③ 并发写 governance_events.jsonl → 事件号撞号 + 哈希链断裂
  ④ 报告/官网硬编码"17 个注册环节"，与 STEP_REGISTRY 实际条数漂移
外加两条交付面回归：trading bundle 的 provides 覆盖全部 preset 技能引用；
白皮书 PDF 必须是可打开的 PDF（不是 base64 文本）。
"""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_system import config
from trading_system.governance_bridge import GovernanceBridge, FiveElementEvent
from trading_system.journal import Journal
from trading_system.options_metrics import OptionsHistoryStore, score_options
from trading_system.providers.base import is_synthetic, source_label
from trading_system.providers.demo import DemoProvider
from trading_system.redline import STEP_REGISTRY
from trading_system.simulator import Bar, SimEngine

ROOT = Path(__file__).resolve().parent.parent
TRADING_BUNDLE = ROOT / "governance" / "bundles" / "trading"


class _RenamedDemo(DemoProvider):
    """典型测试夹具：合成数据源，但 name 不是 "demo"。"""

    name = "probe-demo"


def _pick(ticker: str = "AAPL", **kw) -> SimpleNamespace:
    base = dict(ticker=ticker, card="标准做多", entry_template="回踩确认",
                sector="XLK", chain="ai_compute", entry_price=100.0,
                stop_price=95.0, tss_final=8.0, tos=6.0, shares=10,
                risk_usd=50.0, time_stop_days=7)
    base.update(kw)
    return SimpleNamespace(**base)


def _result(provider: str, picks: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        trade_date="2026-09-29", provider=provider, action="BUY",
        picks=picks if picks is not None else [_pick()],
        mrs=SimpleNamespace(mrs_star=7.5, position_cap=(0.4, 0.7)),
    )


# ---------------------------------------------------------------- ① 来源标记

def test_synthetic_flag_survives_rename():
    assert is_synthetic(DemoProvider()) is True
    assert is_synthetic(_RenamedDemo()) is True          # 子类改名不再绕过
    assert is_synthetic(SimpleNamespace(name="demo")) is True
    assert is_synthetic(SimpleNamespace(name="onestale-demo")) is True
    assert is_synthetic(SimpleNamespace(name="yahoo", synthetic=False)) is False
    assert is_synthetic(SimpleNamespace(name="tencent", synthetic=False)) is False


def test_source_label_normalizes_synthetic_variants():
    assert source_label("demo") == "demo"
    assert source_label("onestale-demo") == "demo"
    assert source_label("yahoo") == "yahoo"


def test_journal_and_sim_stamp_data_source(tmp_path):
    j = Journal(tmp_path / "journal.json")
    assert j.log_picks(_result("demo")) == 1
    assert j.records[0]["source"] == "demo"
    j2 = Journal(tmp_path / "journal2.json")
    assert j2.log_picks(_result("tencent")) == 1
    assert j2.records[0]["source"] == "tencent"

    sim = SimEngine(str(tmp_path / "sim.json"))
    sim.step("2026-09-29", _result("demo", [_pick("MSFT")]),
             lambda t: Bar(open=100.0, high=101.0, low=99.0, close=100.5, adv=1e9))
    assert sim.state["pending"][0]["source"] == "demo"
    assert sim.state["ops_log"][-1]["source"] == "demo"


# ---------------------------------------------------------------- ② 期权历史库

def test_synthetic_subclass_never_writes_option_history(monkeypatch, tmp_path):
    """合成数据源（含改名子类）不得在默认 root 留下任何文件。"""
    monkeypatch.delenv("TS_OPTIONS_HIST_DIR", raising=False)
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path))
    out = score_options("PROBE", _RenamedDemo())
    assert out is not None
    assert not (tmp_path / "options_hist" / "PROBE.json").exists()


def test_real_provider_still_persists_option_history(monkeypatch, tmp_path):
    """正负对照：真实数据源仍按原口径落盘（修复不得误伤正常链路）。"""
    monkeypatch.delenv("TS_OPTIONS_HIST_DIR", raising=False)
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path))
    snap = {"pcr_oi": 1.0, "atm_iv": 0.5, "call_oi": 1000.0}

    class _Real:
        name = "tencent"
        synthetic = False

        def options_chain_snapshot(self, ticker):
            return dict(snap)

    score_options("REAL", _Real())
    stored = json.loads((tmp_path / "options_hist" / "REAL.json").read_text())
    assert len(stored) == 1 and stored[0]["atm_iv"] == 0.5


def test_default_store_uses_config_reports_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("TS_OPTIONS_HIST_DIR", raising=False)
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path))
    assert OptionsHistoryStore().root == tmp_path / "options_hist"


# ---------------------------------------------------------------- ③ 事件账并发

def _emit_batch(args: tuple[str, int, int]) -> None:
    path, worker, n = args
    bridge = GovernanceBridge(path)
    for i in range(n):
        bridge.emit(FiveElementEvent(
            who={"type": "agent", "id": f"w{worker}"},
            context={"stage": "paper"},
            object={"type": "report", "id": "concurrency-test"},
            decision={"action": f"probe.{worker}.{i}"},
        ))


def test_concurrent_emit_keeps_hash_chain(tmp_path):
    """4 进程并发写事件账：不得撞号、不得断链（修复前 100 条里 29 条重复）。"""
    path = tmp_path / "governance_events.jsonl"
    workers, per_worker = 4, 10
    ctx = mp.get_context("fork" if "fork" in mp.get_all_start_methods() else "spawn")
    with ctx.Pool(workers) as pool:
        pool.map(_emit_batch, [(str(path), w, per_worker) for w in range(workers)])

    records = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    ids = [r["payload"]["event_id"] for r in records]
    assert len(records) == workers * per_worker
    assert len(set(ids)) == len(ids)                     # 无撞号
    ok, errors = GovernanceBridge.verify_chain(str(path))
    assert ok, errors[:3]


# ---------------------------------------------------------------- ④ demo 隔离

def test_demo_out_dir_defaults_to_isolated_dir(monkeypatch, tmp_path):
    import main

    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path))
    assert main._resolve_out_dir(str(tmp_path), synthetic=True) == str(tmp_path / "demo")
    assert main._resolve_out_dir(str(tmp_path), synthetic=False) == str(tmp_path)
    # 显式指定的目录不被改写（调用方负责，且仍受 live-ledger 闸门约束）
    assert main._resolve_out_dir(str(tmp_path / "custom"), synthetic=True) == str(tmp_path / "custom")


def test_live_ledger_gate_fails_closed(tmp_path):
    import main

    assert main._looks_like_live_ledger(str(tmp_path)) == []

    (tmp_path / "journal.json").write_text(json.dumps([{"date": "2026-09-01", "ticker": "X"}]))
    assert "journal.json" in main._looks_like_live_ledger(str(tmp_path))   # 无 source = 真实台账

    (tmp_path / "journal.json").write_text(json.dumps([{"date": "2026-09-01", "ticker": "X",
                                                        "source": "demo"}]))
    assert main._looks_like_live_ledger(str(tmp_path)) == []

    (tmp_path / "sim_portfolio.json").write_text(json.dumps(
        {"positions": [], "pending": [{"ticker": "X", "source": "tencent"}],
         "closed": [], "ops_log": []}))
    assert "sim_portfolio.json" in main._looks_like_live_ledger(str(tmp_path))


# ---------------------------------------------------------------- ⑤ 文案口径

def test_philosophy_tab_uses_registry_count():
    from trading_system.report_html import _philosophy_tab

    html = _philosophy_tab()
    assert f"{len(STEP_REGISTRY)} 个注册环节" in html
    assert "17 个注册环节" not in html                    # 漂移口径不得复活


# ---------------------------------------------------------------- ⑥ bundle 一致性

def test_bundle_provides_covers_all_preset_skills():
    """每个 preset 引用的技能都必须在 provides.skills 声明（否则装配后技能缺失）。"""
    import yaml

    manifest = json.loads((TRADING_BUNDLE / "bundle.json").read_text())
    provides = manifest["workloom"]["provides"]
    declared = {p.split("/")[1] for p in provides["skills"] if p.startswith("skills/")}

    referenced: set[str] = set()
    for preset in sorted((TRADING_BUNDLE / "presets").glob("*.y*ml")):
        doc = yaml.safe_load(preset.read_text()) or {}
        referenced.update(doc.get("skills") or [])
    assert referenced <= declared, f"preset 引用了未声明技能: {sorted(referenced - declared)}"

    on_disk = {p.parent.name for p in (TRADING_BUNDLE / "skills").glob("*/SKILL.md")}
    assert on_disk == declared, f"磁盘与提供清单不一致: {sorted(on_disk ^ declared)}"
    for rel in provides["skills"] + provides["presets"] + provides["fences"]:
        assert (TRADING_BUNDLE / rel).is_file(), f"声明资产缺失: {rel}"


def test_whitepaper_files_are_valid_pdfs():
    """白皮书必须是真的 PDF（修复前是 5.4MB base64 文本，下载打不开）。"""
    for rel in ("reports/AI短线美股交易白皮书_20260730.pdf",
                "site/AI短线美股交易白皮书_20260730.pdf"):
        head = (ROOT / rel).read_bytes()[:5]
        assert head == b"%PDF-", f"{rel} 不是有效 PDF（头部 {head!r}）"
