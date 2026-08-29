"""内核 → 治理外壳 五元事件桥接（S5 任务三）。

把交易内核的关键动作（pipeline 每环节结束、L4 闸门判定、模拟盘成交、
合规校验命中）翻译为 WorkLoom 五元事件，append-only 写入
reports/governance_events.jsonl（或 --out 指定目录），并维护
SHA-256 哈希链（防篡改留痕）。

对齐口径：
  - 五元字段：governance/packages/shared/src/event-schema.ts（BusinessEventSchema）
    Who(human/agent/system + id + version?) × Context(tenant/workspace/time/channel/
    market/stage) × Object(type+id，8 类对齐 bundles/trading/schemas/objects.json)
    × Decision(action/before/after/basis/memory_refs) × RuleImpact[](rule_id/version/result)
  - 哈希链：governance/packages/base/workdata/events.ts 的
    sha256(prev_hash ‖ canonicalJson(payload))，首条 prev_hash = "GENESIS"；
    canonicalJson = 键序稳定、剔除 undefined（None）、无空白分隔的 JSON。

边界（红线合规说明）：
  本模块是【治理旁路】——只读内核产出做留痕翻译，绝不回写任何决策输入；
  内核决策不依赖本模块（失败仅记 WARNING，不阻塞 pipeline），因此不违反
  "外壳只做治理不做决策"与"内核零改写"红线。事件文件属会计账白名单
  （state.WHITELIST），跨轮累计、零基线不清除。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

GENESIS_HASH = "GENESIS"          # 对齐 governance workdata events.ts
FENCE_VERSION = "trading-baseline/v1"
_EVENT_SEQ_BASE = 8800            # 对齐底座种子编号段（E-88xx），运行时自然续接
_CST = timezone(timedelta(hours=8))   # 事件时间用 +08:00（附录 E 示例口径）


def canonical_json(value: Any) -> str:
    """规范化序列化（键序稳定），与 governance canonicalJson 逐字节对齐：
    - None → null；dict 跳过 None 值（≈ JS undefined）并按键名排序；
    - bool → true/false；整数值的 float → 整数形式（JS Number 语义）；
    - 字符串不转义非 ASCII（JS JSON.stringify 原样输出 Unicode）。
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value) and abs(value) < 2**53:
            return str(int(value))
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted((k, v) for k, v in value.items() if v is not None)
        return "{" + ",".join(
            f"{json.dumps(k, ensure_ascii=False)}:{canonical_json(v)}"
            for k, v in items) + "}"
    raise TypeError(f"canonical_json 不支持的类型: {type(value)}")


def event_hash(prev_hash: str, payload: dict) -> str:
    """sha256(prev_hash ‖ canonicalJson(payload))，对齐 events.ts eventHash。"""
    return hashlib.sha256(
        (prev_hash + canonical_json(payload)).encode("utf-8")).hexdigest()


@dataclass
class RuleImpact:
    """命中规则记录（event-schema.ts RuleImpactSchema）。"""
    rule_id: str
    version: str = FENCE_VERSION
    result: str = "pass"          # pass | review | blocked | conflict

    def to_dict(self) -> dict:
        return {"rule_id": self.rule_id, "version": self.version,
                "result": self.result}


@dataclass
class FiveElementEvent:
    """五元事件（event-schema.ts BusinessEventSchema 的 Python 对应）。

    who:      {"type": "human"|"agent"|"system", "id": str, "version"?: str}
    context:  {"tenant_id","workspace_id","time"(ISO+offset),
               "channel"?,"market"?,"stage"?}（行业扩展字段走 loose 位）
    object:   {"type": 8 类之一, "id"?: str}
    decision: {"action","before"?,"after"?,"basis"?,"memory_refs"?}
    rule_impact: [RuleImpact]
    """
    who: dict
    context: dict
    object: dict
    decision: dict
    rule_impact: list[RuleImpact] = field(default_factory=list)
    links: list[str] | None = None
    event_id: str = ""

    def to_payload(self) -> dict:
        """序列化为 event-schema.ts 对齐的 payload dict（不含 hash 字段）。"""
        payload: dict[str, Any] = {
            "event_id": self.event_id,
            "who": self.who,
            "context": self.context,
            "object": self.object,
            "decision": self.decision,
            "rule_impact": [r.to_dict() for r in self.rule_impact],
        }
        if self.links:
            payload["links"] = self.links
        return payload


def _now_iso() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


class GovernanceBridge:
    """治理桥：emit 五元事件到 append-only JSONL + 哈希链。

    默认开启；所有异常只记 WARNING 并返回 None——桥接是治理旁路，
    决策内核不依赖它，失败绝不阻塞 pipeline（见模块头注边界说明）。
    """

    def __init__(self, path: str, tenant_id: str = "tiger",
                 workspace_id: str = "trading", enabled: bool = True):
        self.path = path
        self.tenant_id = tenant_id
        self.workspace_id = workspace_id
        self.enabled = enabled

    # ------------------------------------------------------------ 写路径
    def _tail(self) -> tuple[int, str]:
        """读链尾（seq, hash）；文件不存在/为空 → (SEQ_BASE, GENESIS)。"""
        if not os.path.exists(self.path):
            return _EVENT_SEQ_BASE, GENESIS_HASH
        seq, prev = _EVENT_SEQ_BASE, GENESIS_HASH
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    prev = rec["hash"]
                    seq = max(seq, int(rec["payload"]["event_id"].split("-")[1]))
                except Exception:
                    continue
        return seq, prev

    def emit(self, event: FiveElementEvent) -> dict | None:
        """追加一条事件（分配 E-N、计算哈希链接龙）。异常只记 WARNING 返回 None。"""
        if not self.enabled:
            return None
        try:
            seq, prev = self._tail()
            event.event_id = f"E-{seq + 1}"
            event.context.setdefault("tenant_id", self.tenant_id)
            event.context.setdefault("workspace_id", self.workspace_id)
            event.context.setdefault("time", _now_iso())
            payload = event.to_payload()
            h = event_hash(prev, payload)
            rec = {"payload": payload, "prev_hash": prev, "hash": h}
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return rec
        except Exception as exc:  # 治理旁路：失败不阻塞内核
            logger.warning("[治理桥] 事件写入失败（不阻塞内核）: %s", exc)
            return None

    # ------------------------------------------------------------ 读路径
    @staticmethod
    def verify_chain(path: str) -> tuple[bool, list[str]]:
        """校验哈希链连续性与完整性（verify-chain.ts 同口径）。

        逐条重算 sha256(prev_hash ‖ canonicalJson(payload))：
        prev_hash 断链或 hash 重算不符（篡改/口径漂移）即失败。
        返回 (ok, errors)。
        """
        errors: list[str] = []
        if not os.path.exists(path):
            return False, [f"事件文件不存在: {path}"]
        prev = GENESIS_HASH
        n = 0
        with open(path, encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                n += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"第 {ln} 行 JSON 损坏: {exc}")
                    continue
                eid = rec.get("payload", {}).get("event_id", f"line{ln}")
                if rec.get("prev_hash") != prev:
                    errors.append(f"{eid}: prev_hash 断链")
                expect = event_hash(prev, rec.get("payload", {}))
                if rec.get("hash") != expect:
                    errors.append(f"{eid}: hash 重算不符（payload 篡改或口径漂移）")
                prev = rec.get("hash", prev)
        if n == 0:
            errors.append("事件文件为空")
        return (not errors), errors

    # ------------------------------------------------------------ 内核事件翻译
    def emit_from_result(self, result, sim_out: dict | None = None) -> int:
        """把一次 pipeline 结果翻译为五元事件序列。返回成功写入条数。

        事件类型：
          ① pipeline 每环节结束（raw.redline 逐环节）→ object=report
          ② L4 闸门判定（picks + 闸门依据）→ object=signal，R-T10/R-T15 RuleImpact
          ③ 模拟盘成交（sim_out.ops 非空时）→ object=order/portfolio
          ④ 合规校验命中（raw.compliance）→ object=risk_event
        """
        emitted = 0
        raw = getattr(result, "raw", {}) or {}
        market = (raw.get("market") or {}).get("market_id", "us")
        base_ctx = {"tenant_id": self.tenant_id,
                    "workspace_id": self.workspace_id,
                    "time": _now_iso(),
                    "channel": "pipeline",
                    "market": market,
                    "stage": "paper"}

        # ① 每环节结束事件（与内核 ExecutionTracer 双留痕互验）
        for step in raw.get("redline", []):
            status = step.get("status", "executed")
            ev = FiveElementEvent(
                who={"type": "agent", "id": step["step"], "version": "v6.3"},
                context=dict(base_ctx),
                object={"type": "report", "id": result.trade_date},
                decision={
                    "action": f"pipeline.step.{step['step']}",
                    "after": {"status": status, "ms": step.get("ms", 0)},
                    "basis": ([f"环节状态: {status}"]
                              + ([f"透传原因: {step['note']}"] if step.get("note") else [])),
                },
                rule_impact=[RuleImpact(
                    "R-T15", result=("pass" if status == "executed" else "review"))],
            )
            if self.emit(ev):
                emitted += 1

        # ② L4 闸门判定事件
        mrs_star = getattr(getattr(result, "mrs", None), "mrs_star", None)
        picks = [getattr(p, "ticker", str(p)) for p in getattr(result, "picks", [])]
        gate_rules = [RuleImpact("R-T10", result=(
            "blocked" if (mrs_star is not None and mrs_star < 4.0 and picks)
            else "pass"))]
        rationale = raw.get("pick_rationale", {}) or {}
        basis = [f"action={result.action}", f"MRS*={mrs_star}",
                 f"picks={len(picks)}: {', '.join(picks) or '（空）'}"]
        for t in picks[:5]:
            rat = rationale.get(t)
            if isinstance(rat, dict) and rat.get("gate"):
                basis.append(f"{t} 闸门: {json.dumps(rat['gate'], ensure_ascii=False)[:200]}")
        ev = FiveElementEvent(
            who={"type": "agent", "id": "risk-manager", "version": "v6.3"},
            context=dict(base_ctx),
            object={"type": "signal", "id": result.trade_date},
            decision={
                "action": "gate.l4",
                "after": {"action": result.action, "picks": picks,
                          "mrs_star": mrs_star},
                "basis": basis,
                "memory_refs": [f"reports/result_{result.trade_date}.json"],
            },
            rule_impact=gate_rules,
            links=None,
        )
        if self.emit(ev):
            emitted += 1

        # ③ 模拟盘成交事件（有操作才记；ops 文本进 basis，不解析回决策）
        if sim_out and sim_out.get("ops"):
            ev = FiveElementEvent(
                who={"type": "system", "id": "sim-engine", "version": "v6.3"},
                context=dict(base_ctx),
                object={"type": "portfolio", "id": "sim"},
                decision={
                    "action": "sim.fill",
                    "after": {"equity": sim_out.get("equity")},
                    "basis": [str(op) for op in sim_out["ops"]],
                    "memory_refs": ["reports/sim_portfolio.json"],
                },
                rule_impact=[RuleImpact("R-T1", result="pass")],
            )
            if self.emit(ev):
                emitted += 1

        # ④ 合规校验命中事件（逐票）
        for c in raw.get("compliance", []):
            allowed = bool(c.get("allowed", True))
            ev = FiveElementEvent(
                who={"type": "agent", "id": "compliance-officer", "version": "v0.1"},
                context=dict(base_ctx),
                object={"type": "risk_event",
                        "id": f"{c.get('ticker', '?')}:{result.trade_date}"},
                decision={
                    "action": "compliance.check",
                    "after": {"ticker": c.get("ticker"), "side": c.get("side"),
                              "allowed": allowed},
                    "basis": [c.get("reason", "")] if c.get("reason") else [],
                },
                rule_impact=[RuleImpact(
                    c.get("rule_id") or "R-T8",
                    result=("pass" if allowed else "blocked"))],
            )
            if self.emit(ev):
                emitted += 1

        return emitted
