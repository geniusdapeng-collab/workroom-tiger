/** Tiger fence contracts; actual PG lifecycle/locking cases live in scripts/tiger-governance.test.ts. */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { evalCondition, FenceEvalError } from "./expr.js";
import { judge, judgeSubCall, type RuntimeRule } from "./judge.js";
import { checkMonotonic, loadFencePack } from "./dsl.js";
import { fenceActivationFromProposal, fenceRuleRowId, ruleVersionNumber } from "./lifecycle.js";

const bundle = join(dirname(fileURLToPath(import.meta.url)), "../../../bundles/trading");
const pack = loadFencePack(readFileSync(join(bundle, "fences/trading-baseline.yml"), "utf8"));
const risk = JSON.parse(readFileSync(join(bundle, "schemas/risk-defaults.json"), "utf8"));

describe("表达式沙箱", () => {
  it("算术与阈值比较保持边界", () => {
    expect(evalCondition("params.risk_pct > 0.008", { params: { risk_pct: 0.008 } })).toBe(false);
    expect(evalCondition("params.risk_pct > 0.008", { params: { risk_pct: 0.009 } })).toBe(true);
    expect(evalCondition("abs(after.value - before.value) / before.value <= 0.08", { before: { value: 400 }, after: { value: 420 } })).toBe(true);
    expect(evalCondition("params.amount >= 500 and params.executed != true", { params: { amount: 500, executed: false } })).toBe(true);
  });
  it("缺字段、除零、代码执行和类型错误均失败关闭", () => {
    for (const condition of ["after.value / 0 > 1", "after.missing > 1", "process.exit(1)", "'a' + 1 > 0"]) {
      expect(() => evalCondition(condition, { after: { value: 1 } })).toThrow(FenceEvalError);
    }
    expect(evalCondition("", {})).toBe(true);
  });
});

describe("真实 Tiger 生成基线", () => {
  it("paper 研究管线 auto，其他阶段保守 review", () => {
    const input = { object: { type: "report" }, action: "pipeline.daily", context: { stage: "paper" } };
    expect(judge(input, pack.rules, pack.defaultLevel).level).toBe("auto");
    expect(judge({ ...input, context: { stage: "live" } }, pack.rules, pack.defaultLevel).level).toBe("review");
  });
  it("实际风险资产阈值超限 block，等于阈值不误判为超限", () => {
    const input = { object: { type: "order" }, action: "order.buy", context: { market: "us" },
      params: { risk_pct: risk.account.risk_per_trade_pct, change_pct: 0, limit_pct: .1, vcm_cooling: false } };
    expect(judge(input, pack.rules, pack.defaultLevel).level).toBe("review");
    const over = judge({ ...input, params: { ...input.params, risk_pct: risk.account.risk_per_trade_pct + .0001 } }, pack.rules, pack.defaultLevel);
    expect(over.level).toBe("block");
    expect(over.impacts).toContainEqual({ rule_id: "R-T2", version: pack.version, result: "blocked" });
  });
  it("CN 同日回转被阻止，US 由默认审批处理", () => {
    const input = { object: { type: "order" }, action: "order.sell", params: { same_day_buy: true } };
    expect(judge({ ...input, context: { market: "cn" } }, pack.rules, pack.defaultLevel).level).toBe("block");
    expect(judge({ ...input, context: { market: "us" } }, pack.rules, pack.defaultLevel).level).toBe("review");
  });
  it("全源失效或环节缺失禁止报告发布", () => {
    const input = { object: { type: "report" }, action: "report.emit" };
    expect(judge({ ...input, context: { data_all_down: true, steps_missing: 0 } }, pack.rules, pack.defaultLevel).level).toBe("block");
    expect(judge({ ...input, context: { data_all_down: false, steps_missing: 1 } }, pack.rules, pack.defaultLevel).level).toBe("block");
    expect(judge({ ...input, context: { data_all_down: false, steps_missing: 0 } }, pack.rules, pack.defaultLevel).level).toBe("review");
  });
  it("求值异常 block，子调用与主调用同判定", () => {
    const weird: RuntimeRule = { rule_id: "R99", version: "test", name: "缺字段测试", level: "review", is_baseline: false,
      objectTypes: ["report"], actions: ["report.read"], when: "params.missing.deep > 1" };
    const failed = judge({ object: { type: "report" }, action: "report.read", params: {} }, [weird], "auto");
    expect(failed.level).toBe("block");
    expect(failed.evalErrors).toHaveLength(1);
    const input = { object: { type: "report" }, action: "report.emit", context: { data_all_down: true, steps_missing: 0 } };
    expect(judgeSubCall(input, pack.rules, pack.defaultLevel)).toEqual(judge(input, pack.rules, pack.defaultLevel));
  });
});

describe("基线单调守卫", () => {
  it("block 降为 review、删除、缩窄覆盖或改写条件均拒绝", () => {
    const baseline = pack.rules.filter(rule => rule.is_baseline);
    for (const changed of [
      baseline.map(rule => rule.rule_id === "R-T2" ? { ...rule, level: "review" as const } : rule),
      baseline.filter(rule => rule.rule_id !== "R-T2"),
      baseline.map(rule => rule.rule_id === "R-T2" ? { ...rule, actions: ["order.buy"] } : rule),
      baseline.map(rule => rule.rule_id === "R-T2" ? { ...rule, when: "false" } : rule),
    ]) expect(checkMonotonic(baseline, changed).ok).toBe(false);
  });
  it("auto 加严为 review 保留覆盖与条件时通过", () => {
    expect(checkMonotonic(pack.rules, pack.rules.map(rule => rule.rule_id === "R-T0" ? { ...rule, level: "review" as const } : rule)).ok).toBe(true);
  });
});

describe("审批激活绑定必须保留实际版本", () => {
  const workspace = "mc140-unit";
  it("行身份每个实际版本均独立，解析历史版本号不复用", () => {
    expect(fenceRuleRowId("R92", workspace, "v1")).toBe("fr-r92-v1-mc140-unit");
    expect(fenceRuleRowId("R92", workspace, "v2")).toBe("fr-r92-v2-mc140-unit");
    expect(ruleVersionNumber("trading-baseline/v1")).toBe(1);
    expect(ruleVersionNumber("v2")).toBe(2);
    expect(ruleVersionNumber("v-next")).toBeNull();
  });
  it("精确行 ID 或已留痕版本可以绑定；缺版本旧事件失败关闭", () => {
    const decision = { action: "fence.rule.propose", after: { ruleId: "R92", dryRunId: "fdr-r92-test", ruleRowId: "fr-r92-v2-mc140-unit", version: "v2", allowWhenChange: true } };
    expect(fenceActivationFromProposal({ decision }, workspace)).toEqual({ ruleRowId: "fr-r92-v2-mc140-unit", dryRunId: "fdr-r92-test", allowWhenChange: true });
    expect(fenceActivationFromProposal({ decision: { ...decision, after: { ruleId: "R92", dryRunId: "fdr-r92-test", version: "v1" } } }, workspace)).toEqual({ ruleRowId: "fr-r92-v1-mc140-unit", dryRunId: "fdr-r92-test" });
    expect(fenceActivationFromProposal({ decision: { ...decision, after: { ruleId: "R92", dryRunId: "fdr-r92-test" } } }, workspace)).toBeNull();
    expect(fenceActivationFromProposal({ decision: { action: "report.read", after: decision.after } }, workspace)).toBeNull();
    expect(fenceActivationFromProposal(null, workspace)).toBeNull();
  });
});
