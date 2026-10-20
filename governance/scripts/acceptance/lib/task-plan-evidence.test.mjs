import assert from "node:assert/strict";
import { test } from "node:test";
import { latestTaskPlanResponse, taskPlanEvidence, waitForTaskPlan } from "./task-plan-evidence.mjs";

const plan = [{ stepId: "s2", label: "核对实际工作记录" }, { stepId: "s1", label: "【参数待补全】形成复盘摘要" }];
const detail = { procedure: "threads.get", ok: true, requestAt: 200, at: 250,
  input: { threadId: "T-1" }, data: { id: "T-1", title: "当次派发目标", plan_state: "ready", plan } };
const dom = { healthy: true, visible: true, stale: false, pathname: "/tasks/T-1", threadId: "T-1", labels: plan.map((step) => step.label) };
const base = { detail, dom, threadId: "T-1", title: "当次派发目标", navigationAt: 200, deadline: 1000, expectedPath: "/tasks/T-1" };

test("canonical labels, original order and complete warnings are evidence", () => {
  assert.deepEqual(taskPlanEvidence(base), { detail, plan, planVisible: true });
  assert.equal(taskPlanEvidence({ ...base, dom: { ...dom, labels: ["  核对实际工作记录\n", plan[1].label] } }).planVisible, true);
});
test("nonempty single-character labels follow the DTO contract with exact list binding", () => {
  const steps = [{ stepId: "s1", label: "读" }, { stepId: "s2", label: "写" }];
  const oneCharacter = { ...base, detail: { ...detail, data: { ...detail.data, plan: steps } },
    dom: { ...dom, labels: ["读", "写"] } };
  assert.equal(taskPlanEvidence(oneCharacter).planVisible, true);
  assert.equal(taskPlanEvidence({ ...oneCharacter, dom: { ...dom, labels: ["写", "读"] } }).planVisible, false);
  assert.equal(taskPlanEvidence({ ...oneCharacter, detail: { ...detail, data: { ...detail.data, plan: [{ stepId: "s1", label: " \n " }] } },
    dom: { ...dom, labels: [" \n "] } }).planVisible, false);
});
for (const [name, changes] of [
  ["wrong request thread", { detail: { ...detail, input: { threadId: "T-2" } } }],
  ["wrong returned thread", { detail: { ...detail, data: { ...detail.data, id: "T-2" } } }],
  ["wrong target", { detail: { ...detail, data: { ...detail.data, title: "旧目标" } } }],
  ["stale request", { detail: { ...detail, requestAt: 199 } }],
  ["late response", { detail: { ...detail, at: 1001 } }],
  ["failed response", { detail: { ...detail, ok: false } }],
  ["pending plan", { detail: { ...detail, data: { ...detail.data, plan_state: "pending" } } }],
  ["progress only", { detail: { ...detail, data: { id: "T-1", title: "当次派发目标", progress_done: 2, progress_total: 2 } } }],
  ["empty plan", { detail: { ...detail, data: { ...detail.data, plan: [] } } }],
  ["malformed plan", { detail: { ...detail, data: { ...detail.data, plan: [{ stepId: "s1", action: "真实执行" }] } } }],
  ["duplicate IDs", { detail: { ...detail, data: { ...detail.data, plan: [plan[0], plan[0]] } } }],
  ["hidden plan", { dom: { ...dom, visible: false } }],
  ["old cached plan", { dom: { ...dom, stale: true } }],
  ["wrong DOM thread", { dom: { ...dom, threadId: "T-2" } }],
  ["wrong route", { dom: { ...dom, pathname: "/tasks/T-2" } }],
  ["unhealthy page", { dom: { ...dom, healthy: false } }],
  ["missing visible label", { dom: { ...dom, labels: [plan[0].label] } }],
  ["reordered list", { dom: { ...dom, labels: [...dom.labels].reverse() } }],
  ["stripped warning", { dom: { ...dom, labels: [plan[0].label, "形成复盘摘要"] } }],
]) test(`rejects ${name}`, () => { assert.equal(taskPlanEvidence({ ...base, ...changes }).planVisible, false); });

function timedFixture({ availableAt = 600, startsAt = 250, domDelay = 0 } = {}) {
  let time = startsAt;
  const slept = [], observed = [];
  const watcher = { get responses() {
    const row = { ...detail, requestAt: Math.max(200, availableAt - 10), at: availableAt,
      data: { ...detail.data, ...(time >= availableAt ? {} : { plan: null, plan_state: "pending" }) } };
    observed.push(row);
    return [row];
  } };
  const options = { ...base, watcher, dispatchAt: 100, now: () => time,
    readDom: async () => { time += domDelay; return { ...dom, labels: time >= availableAt ? dom.labels : [] }; },
    sleep: async (ms) => { slept.push(ms); time += ms; } };
  return { options, slept, observed, time: () => time };
}
test("observes a later page poll within the original dispatch deadline", async () => {
  const f = timedFixture();
  const result = await waitForTaskPlan(f.options);
  assert.equal(result.planVisible, true);
  assert.equal(result.dispatchMs, 550);
  assert.ok(f.observed.length > 1);
  assert.deepEqual(f.slept, [100, 100, 100, 100]);
});
test("late readiness cannot start a second budget", async () => {
  const f = timedFixture({ availableAt: 1001 });
  const result = await waitForTaskPlan(f.options);
  assert.equal(result.planVisible, false);
  assert.equal(result.dispatchMs, null);
  assert.equal(f.time(), 1000);
  assert.equal(f.slept.at(-1), 50);
});
test("DOM readiness after the deadline fails even with an earlier response", async () => {
  const f = timedFixture({ availableAt: 700, startsAt: 950, domDelay: 100 });
  const result = await waitForTaskPlan(f.options);
  assert.equal(result.planVisible, false);
  assert.equal(result.dispatchMs, null);
  assert.deepEqual(f.slept, []);
});
test("no budget remains after late navigation; no observation or query is invented", async () => {
  const f = timedFixture({ startsAt: 1200 });
  assert.deepEqual(await waitForTaskPlan({ ...f.options, navigationAt: 1200 }), { detail: null, plan: [], planVisible: false, dispatchMs: null });
  assert.equal(f.observed.length, 0);
});
test("read errors propagate instead of becoming empty successful evidence", async () => {
  const f = timedFixture();
  await assert.rejects(waitForTaskPlan({ ...f.options, readDom: async () => { throw new Error("DOM lost"); } }), /DOM lost/);
});
test("a late old pending response cannot hide the newer rendered plan at the deadline", async () => {
  const ready = { ...detail, requestAt: 800, at: 850 };
  const pending = { ...detail, requestAt: 200, at: 950, data: { ...detail.data, plan_state: "pending", plan: null } };
  const responses = [ready, pending]; // Completion order, exactly like observeResponses.
  assert.equal(latestTaskPlanResponse(responses, "T-1", 200), ready);
  const f = timedFixture({ startsAt: 950 });
  const result = await waitForTaskPlan({ ...f.options, watcher: { responses }, readDom: async () => dom });
  assert.equal(result.planVisible, true);
  assert.equal(result.dispatchMs, 850);
});
for (const [name, patch] of [
  ["pending", { data: { ...detail.data, plan_state: "pending", plan: null } }],
  ["failed", { ok: false, data: null }],
  ["invalid", { data: { ...detail.data, plan_state: "invalid", plan: null } }],
]) test(`a newer ${name} poll suppresses old ready evidence even when the old poll finishes last`, async () => {
  const newer = { ...detail, requestAt: 800, at: 850, ...patch };
  const old = { ...detail, requestAt: 200, at: 950 };
  const responses = [newer, old];
  assert.equal(latestTaskPlanResponse(responses, "T-1", 200), newer);
  const f = timedFixture({ startsAt: 950 });
  const result = await waitForTaskPlan({ ...f.options, watcher: { responses }, readDom: async () => dom });
  assert.equal(result.planVisible, false);
  assert.equal(result.dispatchMs, null);
});
