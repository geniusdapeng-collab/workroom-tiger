import assert from "node:assert/strict";
import { test } from "node:test";
import { REQUIRED_PROFILE, runtimeEnvironment, validateRequest, validateEnvelope, invokeWorkflow, exitCode } from "./workflow-client.mjs";
import { REQUEST, RUN_ID, result, runnerFixture } from "../../../scripts/tiger-agent-test-support.mjs";

const stages = ["clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "decision.debate"];
function modelReceipt() {
  const receipt = result("receipt");
  receipt.summary.disclosures.model_mode = "enabled";
  receipt.summary.disclosures.model_required = true;
  receipt.summary.model = { provider: "deepseek", model: "deepseek-flash", release: "DeepSeek-V4.1-Flash-1017",
    required: true, response_ids: stages.map((_, index) => `fixture-response-${index}`), call_count: 5,
    required_stages: [...stages], stage_counts: Object.fromEntries(stages.map(stage => [stage, 1])), search_count: 14,
    search_mode: "provider_native_search", all_required_completed: true };
  receipt.summary.action_summary = "传输夹具：等待，不代表真实研究。";
  receipt.summary.execution_decisions = [];
  return receipt;
}

test("required profile is explicit and cannot take provider, credential or model overrides", () => {
  assert.equal(validateRequest("run", { ...REQUEST, profile: REQUIRED_PROFILE }).profile, REQUIRED_PROFILE);
  assert.equal(validateRequest("describe", { profile: REQUIRED_PROFILE }).profile, REQUIRED_PROFILE);
  for (const key of ["LLM_API_KEY", "model", "provider", "model_mode", "search", "env"]) {
    assert.throws(() => validateRequest("run", { ...REQUEST, profile: REQUIRED_PROFILE, [key]: "override" }), { code: "INVALID_REQUEST" });
  }
});

test("trusted required startup keeps exactly four LLM settings and strips unrelated secrets", () => {
  const env = { HOME: "/fixture-home", PATH: "/bin", LLM_BACKEND: "api", LLM_BASE_URL: "https://api.deepseek.com",
    LLM_MODEL: "deepseek-flash", LLM_API_KEY: "fixture-only", OPENAI_API_KEY: "other-key", TIGER_PNL_API_TOKEN: "pnl-key", PYTHONPATH: "/untrusted" };
  assert.equal(runtimeEnvironment(env).LLM_API_KEY, undefined);
  const required = runtimeEnvironment(env, true);
  assert.equal(required.LLM_API_KEY, "fixture-only");
  assert.equal(required.LLM_MODEL, "deepseek-flash");
  assert.equal(required.OPENAI_API_KEY, undefined);
  assert.equal(required.TIGER_PNL_API_TOKEN, undefined);
  assert.equal(required.PYTHONPATH, undefined);
});

test("required startup environment is selected by the fixed profile and run operation", async t => {
  const fixture = await runnerFixture(t);
  const old = Object.fromEntries(["LLM_BACKEND", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"].map(key => [key, process.env[key]]));
  Object.assign(process.env, { LLM_BACKEND: "api", LLM_BASE_URL: "https://api.deepseek.com", LLM_MODEL: "deepseek-flash", LLM_API_KEY: "fixture-only" });
  t.after(() => { for (const [key, value] of Object.entries(old)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; } });
  await invokeWorkflow("run", { ...REQUEST, profile: REQUIRED_PROFILE }, fixture.options);
  await invokeWorkflow("run", REQUEST, fixture.options);
  await invokeWorkflow("describe", { profile: REQUIRED_PROFILE }, fixture.options);
  const calls = await fixture.calls();
  assert.equal(calls[0].envNames.includes("LLM_API_KEY"), true);
  assert.equal(calls[1].envNames.includes("LLM_API_KEY"), false);
  assert.equal(calls[2].envNames.includes("LLM_API_KEY"), false);
});

test("required receipt accepts only completed, exact fixed model and complete unique stage proof", () => {
  const receipt = modelReceipt();
  assert.equal(validateEnvelope(receipt, "receipt", { run_id: RUN_ID }), receipt);
  for (const mutate of [
    row => { row.status = "completed_degraded"; },
    row => { delete row.summary.model; },
    row => { row.summary.model.model = "deepseek-reasoner"; },
    row => { row.summary.model.all_required_completed = false; },
    row => { row.summary.model.response_ids[1] = row.summary.model.response_ids[0]; },
    row => { row.summary.model.stage_counts[stages[0]] = 0; },
    row => { row.summary.model.required_stages.reverse(); },
    row => { row.summary.model.search_count = 0; },
    row => { row.summary.disclosures.model_required = false; },
    row => { delete row.summary.action_summary; },
    row => { delete row.summary.execution_decisions; },
  ]) {
    const invalid = modelReceipt(); mutate(invalid);
    assert.throws(() => validateEnvelope(invalid, "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
  }
  assert.equal(exitCode({ operation_ok: false, error: { code: "MODEL_REQUIRED_FAILED" } }), 8);
});

test("legacy disabled receipts keep their historical completed_degraded contract", () => {
  const legacy = result("receipt", { status: "completed_degraded" });
  assert.equal(validateEnvelope(legacy, "receipt", { run_id: RUN_ID }), legacy);
});
