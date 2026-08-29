"""治理桥测试（S5 任务三）。

① 事件五元字段完整且与 event-schema.ts 对齐；
② 哈希链连续性与篡改检测（改一条历史事件 → 验链失败）；
③ demo 运行产生事件且 RuleImpact 命中记录正确；
④ 桥接异常不阻塞 pipeline（治理旁路）。
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trading_system.governance_bridge import (  # noqa: E402
    GENESIS_HASH, FiveElementEvent, GovernanceBridge, RuleImpact,
    canonical_json, event_hash,
)

OBJECT_TYPES = {"instrument", "signal", "research_note", "position",
                "order", "risk_event", "portfolio", "report"}
RULE_RESULTS = {"pass", "review", "blocked", "conflict"}


def _read_events(path):
    return [json.loads(ln) for ln in Path(path).read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def test_canonical_json_matches_ts_semantics():
    """canonicalJson 与 governance workdata/events.ts 测试向量对齐。"""
    # events.ts 测试向量：{"b":1,"a":{"d":2,"c":3}} → '{"a":{"c":3,"d":2},"b":1}'
    assert canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'
    assert canonical_json(None) == "null"
    assert canonical_json(True) == "true"
    assert canonical_json(1.0) == "1"          # JS Number：1.0 → "1"
    assert canonical_json("中文") == '"中文"'   # JS 不转义非 ASCII
    assert canonical_json({"a": None, "b": [1, "x"]}) == '{"b":[1,"x"]}'  # None≈undefined 剔除


def test_five_element_fields_align_event_schema(tmp_path):
    """五元字段完整且与 event-schema.ts（BusinessEventSchema）对齐。"""
    bridge = GovernanceBridge(str(tmp_path / "events.jsonl"))
    ev = FiveElementEvent(
        who={"type": "agent", "id": "risk-manager", "version": "v6.3"},
        context={"tenant_id": "tiger", "workspace_id": "trading",
                 "time": "2026-08-29T06:00:00+08:00",
                 "channel": "pipeline", "market": "us", "stage": "paper"},
        object={"type": "signal", "id": "2026-08-29"},
        decision={"action": "gate.l4", "after": {"picks": ["AAPL"]},
                  "basis": ["MRS*=7.2"], "memory_refs": ["reports/x.json"]},
        rule_impact=[RuleImpact("R-T10", result="pass")],
    )
    rec = bridge.emit(ev)
    assert rec is not None
    p = rec["payload"]
    # event-schema.ts 顶层字段：event_id/who/context/object/decision/rule_impact
    assert set(p) >= {"event_id", "who", "context", "object", "decision", "rule_impact"}
    assert p["event_id"].startswith("E-")
    assert p["who"]["type"] in {"human", "agent", "system"} and p["who"]["id"]
    ctx = p["context"]
    assert ctx["tenant_id"] and ctx["workspace_id"] and "+08:00" in ctx["time"]
    assert p["object"]["type"] in OBJECT_TYPES
    dec = p["decision"]
    assert dec["action"] and isinstance(dec.get("basis"), list)
    for ri in p["rule_impact"]:
        assert set(ri) == {"rule_id", "version", "result"}
        assert ri["result"] in RULE_RESULTS


def test_hash_chain_continuity_and_tamper_detection(tmp_path):
    """多条事件链连续；篡改任一历史事件 → verify_chain 失败。"""
    path = tmp_path / "events.jsonl"
    bridge = GovernanceBridge(str(path))
    for i in range(5):
        assert bridge.emit(FiveElementEvent(
            who={"type": "system", "id": "t"},
            context={}, object={"type": "report"},
            decision={"action": f"a{i}"})) is not None
    ok, errs = GovernanceBridge.verify_chain(str(path))
    assert ok, errs
    recs = _read_events(path)
    assert recs[0]["prev_hash"] == GENESIS_HASH
    for a, b in zip(recs, recs[1:]):
        assert b["prev_hash"] == a["hash"]          # 严格接龙
        assert b["hash"] == event_hash(a["hash"], b["payload"])
    # 篡改第 2 条 payload（改 action）→ 验链必须失败
    recs[1]["payload"]["decision"]["action"] = "tampered"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                    encoding="utf-8")
    ok, errs = GovernanceBridge.verify_chain(str(path))
    assert not ok and any("重算不符" in e or "断链" in e for e in errs)


def test_demo_run_emits_events_with_rule_impact(tmp_path):
    """demo pipeline 结果翻译为事件：环节事件 R-T15 全 pass，L4 事件 R-T10 正确。"""
    from trading_system.pipeline import run_pipeline
    result = run_pipeline(provider_name="demo", universe_mode="core", top_n=10)
    bridge = GovernanceBridge(str(tmp_path / "events.jsonl"))
    n = bridge.emit_from_result(result, sim_out={"equity": 100000, "ops": ["🟢 买入 X"]})
    assert n > 0
    ok, errs = GovernanceBridge.verify_chain(bridge.path)
    assert ok, errs
    recs = _read_events(bridge.path)
    step_events = [r for r in recs
                   if r["payload"]["decision"]["action"].startswith("pipeline.step.")]
    assert len(step_events) == len(result.raw["redline"])   # 每环节一条
    for r in step_events:
        ri = r["payload"]["rule_impact"][0]
        assert ri["rule_id"] == "R-T15"
        assert ri["result"] == ("pass" if r["payload"]["decision"]["after"]["status"]
                                == "executed" else "review")
    gate = [r for r in recs if r["payload"]["decision"]["action"] == "gate.l4"]
    assert len(gate) == 1
    ri = gate[0]["payload"]["rule_impact"][0]
    assert ri["rule_id"] == "R-T10" and ri["result"] in RULE_RESULTS
    sim = [r for r in recs if r["payload"]["decision"]["action"] == "sim.fill"]
    assert len(sim) == 1 and sim[0]["payload"]["object"]["type"] == "portfolio"


def test_bridge_failure_never_blocks(tmp_path, monkeypatch, caplog):
    """桥接异常只记 WARNING，不抛出、不阻塞调用方（治理旁路边界）。"""
    bridge = GovernanceBridge(str(tmp_path / "nonexist_dir_deep" / "events.jsonl"))
    monkeypatch.setattr(GovernanceBridge, "_tail",
                        lambda self: (_ for _ in ()).throw(RuntimeError("IO 炸")))
    with caplog.at_level("WARNING"):
        rec = bridge.emit(FiveElementEvent(
            who={"type": "system", "id": "t"}, context={},
            object={"type": "report"}, decision={"action": "x"}))
    assert rec is None
    assert any("不阻塞内核" in m for m in caplog.messages)
    # 关闭开关 → 静默不写
    off = GovernanceBridge(str(tmp_path / "off.jsonl"), enabled=False)
    assert off.emit(FiveElementEvent(who={"type": "system", "id": "t"}, context={},
                                     object={"type": "report"},
                                     decision={"action": "x"})) is None
    assert not (tmp_path / "off.jsonl").exists()


def test_emit_from_result_failure_does_not_raise(tmp_path, monkeypatch):
    """emit_from_result 内部 emit 全失败时返回 0，不向上抛。"""
    bridge = GovernanceBridge(str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(GovernanceBridge, "emit", lambda self, ev: None)

    class _R:  # 最小结果替身
        trade_date = "2026-08-29"
        action = "HOLD"
        picks = []
        mrs = None
        raw = {"redline": [{"step": "s1", "status": "executed", "ms": 1}],
               "compliance": [], "market": {"market_id": "us"}}
    assert bridge.emit_from_result(_R(), None) == 0
