import assert from "node:assert/strict";
import { test } from "node:test";
import { LIMITS, invokeWorkflow, waitForReceipt } from "./workflow-client.mjs";
import { RUN_ID, result, runnerFixture } from "../../../scripts/tiger-agent-test-support.mjs";

test("large-run receipt proof has a bounded 90-minute request budget", () => {
  assert.equal(LIMITS.receiptMs, 90 * 60 * 1000);
  assert.equal(LIMITS.maxWaitSeconds, 4 * 60 * 60);
});

test("direct proof operations get the proof budget while status and explicit deadlines keep their limits", async (t) => {
  const fixture = await runnerFixture(t);
  const delays = [];
  const schedule = globalThis.setTimeout;
  t.mock.method(globalThis, "setTimeout", (callback, delay, ...args) => {
    delays.push(delay);
    return schedule(callback, delay, ...args);
  });

  const input = { run_id: RUN_ID };
  for (const operation of ["status", "receipt", "verify"]) {
    const value = await invokeWorkflow(operation, input, fixture.options);
    assert.equal(value.operation_ok, true, operation);
  }
  const explicitTimeoutMs = 5000;
  const value = await invokeWorkflow("verify", input, fixture.options, { timeoutMs: explicitTimeoutMs });
  assert.equal(value.operation_ok, true);
  assert.deepEqual(delays, [LIMITS.requestMs, LIMITS.receiptMs, LIMITS.receiptMs, explicitTimeoutMs]);
});

test("run wait retries a transient status timeout without resubmitting", async () => {
  const calls = [];
  let statuses = 0;
  const invoke = async (operation) => {
    calls.push(operation);
    if (operation === "status" && statuses++ === 0) {
      return {
        ...result("status"),
        operation_ok: false,
        status: "interrupted",
        error: {
          code: "TIMEOUT", message: "status observation exceeded 60 seconds",
          run_id: RUN_ID, stage: null, retryable: true, details_ref: null,
        },
      };
    }
    if (operation === "status") return result("status", { status: "completed" });
    if (operation === "receipt") return result("receipt");
    throw new Error(`unexpected operation ${operation}`);
  };

  const value = await waitForReceipt(RUN_ID, {}, { invoke, pollMs: 1, waitMs: 1000 });
  assert.equal(value.operation, "receipt");
  assert.equal(value.operation_ok, true);
  assert.deepEqual(calls, ["status", "status", "receipt"]);
});

test("run wait still stops immediately for a permanent request error", async () => {
  const calls = [];
  const failure = {
    ...result("status"),
    operation_ok: false,
    status: "invalid",
    error: {
      code: "INVALID_REQUEST", message: "fixed request differs",
      run_id: RUN_ID, stage: null, retryable: false, details_ref: null,
    },
  };
  const value = await waitForReceipt(RUN_ID, {}, {
    pollMs: 1,
    waitMs: 1000,
    invoke: async (operation) => {
      calls.push(operation);
      return failure;
    },
  });
  assert.equal(value.error.code, "INVALID_REQUEST");
  assert.deepEqual(calls, ["status"]);
});

test("run wait retries a transient receipt proof timeout without resubmitting", async () => {
  const calls = [];
  let receipts = 0;
  const invoke = async (operation) => {
    calls.push(operation);
    if (operation === "status") return result("status", { status: "completed" });
    if (operation === "receipt" && receipts++ === 0) {
      return {
        ...result("receipt"),
        operation_ok: false,
        status: "interrupted",
        error: {
          code: "TIMEOUT", message: "receipt proof exceeded the request budget",
          run_id: RUN_ID, stage: null, retryable: false, details_ref: null,
        },
      };
    }
    if (operation === "receipt") return result("receipt");
    throw new Error(`unexpected operation ${operation}`);
  };

  const value = await waitForReceipt(RUN_ID, {}, { invoke, pollMs: 1, waitMs: 1000 });
  assert.equal(value.operation, "receipt");
  assert.equal(value.operation_ok, true);
  assert.deepEqual(calls, ["status", "receipt", "receipt"]);
});

test("run wait starts no receipt proof when terminal status exhausts its deadline", async (t) => {
  let now = 1000;
  t.mock.method(Date, "now", () => now);
  const calls = [];
  const value = await waitForReceipt(RUN_ID, {}, {
    waitMs: 10,
    invoke: async (operation) => {
      calls.push(operation);
      now = 1010;
      return result(operation);
    },
  });

  assert.equal(value.error?.code, "TIMEOUT");
  assert.equal(value.operation, "run");
  assert.equal(value.run_id, RUN_ID);
  assert.equal(value.error.run_id, RUN_ID);
  assert.deepEqual(calls, ["status"]);
});

test("run wait starts no new receipt proof after a transient proof exhausts its deadline", async (t) => {
  let now = 1000;
  t.mock.method(Date, "now", () => now);
  const calls = [];
  const value = await waitForReceipt(RUN_ID, {}, {
    operation: "resume",
    pollMs: 1,
    waitMs: 10,
    invoke: async (operation, input, options, { timeoutMs }) => {
      calls.push({ operation, input, timeoutMs });
      if (operation === "status" || calls.length > 2) return result(operation);
      now = 1010;
      return {
        ...result("receipt"),
        operation_ok: false,
        status: "interrupted",
        error: {
          code: "TIMEOUT", message: "receipt proof exceeded its request budget",
          run_id: RUN_ID, stage: null, retryable: false, details_ref: null,
        },
      };
    },
  });

  assert.equal(value.error?.code, "TIMEOUT");
  assert.equal(value.operation, "resume");
  assert.equal(value.run_id, RUN_ID);
  assert.deepEqual(calls, [
    { operation: "status", input: { run_id: RUN_ID }, timeoutMs: 10 },
    { operation: "receipt", input: { run_id: RUN_ID }, timeoutMs: 10 },
  ]);
});

test("run wait does not start receipt proof after observation is cancelled", async () => {
  const controller = new AbortController();
  const calls = [];
  const value = await waitForReceipt(RUN_ID, {}, {
    signal: controller.signal,
    invoke: async (operation) => {
      calls.push(operation);
      if (operation === "status") controller.abort();
      return result(operation);
    },
  });

  assert.equal(value.error?.code, "CANCELLED");
  assert.equal(value.run_id, RUN_ID);
  assert.deepEqual(calls, ["status"]);
});
