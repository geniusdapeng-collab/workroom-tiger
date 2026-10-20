/** MC024: execute the real gate entry; replace only HTTP/PG/process boundaries. No live database. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Payload = {
  event_id: string;
  who: { type: string; id: string; version?: string };
  context: { tenant_id: string; workspace_id: string; time: string };
  object: { type: string; id?: string };
  decision: Record<string, unknown>;
  rule_impact: unknown[];
  receipt?: Record<string, unknown>;
};
type StoredEvent = { tenant_id: string; workspace_id: string; session_id: string; payload: Payload };
type InstalledAgent = { workspace_id: string; preset_key: string; status: string; readonly: boolean };
type Scenario = {
  dispatch: Record<string, unknown> | null;
  thread: Record<string, unknown> | null;
  apiEvents: unknown;
  ledger: StoredEvent[];
  agents: InstalledAgent[];
  queries: Array<{ sql: string; params: unknown[]; tenant?: unknown; workspace?: unknown }>;
  requests: Array<{ path: string; input: Record<string, unknown> }>;
  failAt?: "threads.dispatch" | "threads.get" | "threads.events" | "ledger";
  timeoutDispatch?: boolean;
  timeoutAt?: "threads.get" | "threads.events";
  briefings: number;
};
const doubles = vi.hoisted(() => ({ current: undefined as unknown as Scenario }));

vi.mock("@workloom/base/captain", () => ({ charterSchema: { safeParse: () => ({ success: true }) } }));
vi.mock("@workloom/base/tenancy", () => ({ signDemoToken: async () => "test-only-token" }));
vi.mock("node:child_process", () => ({
  execSync: () => Buffer.from('===== 结构化验证报告（JSON） =====\n{"ok":true,"workspaces":1,"total_events":3,"total_issues":0}\n'),
}));
vi.mock("pg", () => ({ default: { Client: class {
  tenant?: unknown;
  workspace?: unknown;
  async connect() { return undefined; }
  async end() { return undefined; }
  async query(sql: string, params: unknown[] = []) {
    const state = doubles.current;
    state.queries.push({ sql, params, tenant: this.tenant, workspace: this.workspace });
    if (sql.includes("set_config('app.tenant_id'")) { this.tenant = params[0]; return { rows: [] }; }
    if (sql.includes("set_config('app.workspace_id'")) { this.workspace = params[0]; return { rows: [] }; }
    if (sql.includes("FROM profiles")) return { rows: [{ workspace_id: "ws-a", charter: {} }], rowCount: 1 };
    if (sql.includes("FROM workspaces w")) {
      if (params[0] != null && params[0] !== "ws-a") return { rows: [] };
      // Model the emitted selector predicates. Without the new bound preset filter,
      // the previous gate selects the unrelated default operator and reproduces fallback.
      const bindsPreset = /\bpreset_key\s*=\s*\$2\b/.test(sql);
      const explicitPreset = bindsPreset ? params[1] : null;
      const readonlyOverride = /\$2::text\s+IS NOT NULL\s+OR\s+readonly\s*=\s*false/i.test(sql);
      const candidates = state.agents.filter((agent) => {
        if (/\bworkspace_id\s*=\s*w\.id\b/.test(sql) && agent.workspace_id !== "ws-a") return false;
        if (/status\s+IN\s*\('ready','active'\)/.test(sql) && !["ready", "active"].includes(agent.status)) return false;
        if (explicitPreset != null && agent.preset_key !== explicitPreset) return false;
        if (/readonly\s*=\s*false/.test(sql) && !(readonlyOverride && explicitPreset != null) && agent.readonly) return false;
        return true;
      }).sort((a, b) => a.preset_key.localeCompare(b.preset_key));
      const agent = candidates[0];
      return { rows: agent ? [{
        tenant_id: "tenant-a", id: "ws-a", slug: "sample", name: "验收工作区",
        member_id: "member-a", member_no: "MEM-A", member_name: "验收成员", member_role: "owner",
        plan: "pro", preset_key: agent.preset_key, preset_readonly: agent.readonly,
      }] : [] };
    }
    if (sql.includes("FROM triggers")) return { rows: [{ n: "1" }] };
    if (sql.includes("ceo.briefing")) return { rows: [{ n: String(state.briefings) }] };
    if (sql.includes("FROM biz_events")) {
      if (state.failAt === "ledger") throw new Error("ledger permission denied");
      // Model RLS plus the predicates actually issued by the gate. The old broad query
      // therefore sees unrelated ASK rows and reproduces the original false positive.
      const rows = state.ledger.filter((row) => {
        if (row.tenant_id !== this.tenant || row.workspace_id !== this.workspace) return false;
        for (const field of ["tenant_id", "workspace_id", "session_id"] as const) {
          const parameter = sql.match(new RegExp(`\\b${field}\\s*=\\s*\\$(\\d+)`));
          if (parameter && row[field] !== params[Number(parameter[1]) - 1]) return false;
        }
        return true;
      });
      if (sql.includes("count(DISTINCT session_id)")) {
        return { rows: [{ tc: String(new Set(rows.map((row) => row.session_id)).size), ec: String(rows.length) }] };
      }
      return { rows, rowCount: rows.length };
    }
    throw new Error(`Unexpected SQL in isolated gate test: ${sql}`);
  }
} } }));

const title = "生成一份本周运行复盘，并列出三项下一步任务";
const scope = { tenant_id: "tenant-a", workspace_id: "ws-a" };
function successfulScenario(steps = 2, mode: "simulated" | "real" = "simulated"): Scenario {
  const time = "2026-10-20T00:00:00.000Z";
  const events: Payload[] = [{
    event_id: "E-100", who: { type: "human", id: "MEM-A" }, context: { ...scope, time },
    object: { type: "thread", id: "T-100" },
    decision: { action: "thread.dispatch", after: { threadId: "T-100", title, mode: "quest" } }, rule_impact: [],
  }, ...Array.from({ length: steps }, (_, index): Payload => ({
    event_id: `E-${101 + index}`, who: { type: "agent", id: "operator", version: "v1" }, context: { ...scope, time },
    object: { type: "result" },
    decision: { kind: "execute", action: "result.read", step_id: `s${index + 1}`, tool: "result.read", effect: "read", params: {}, after: { result: { state: mode } } },
    rule_impact: [], receipt: {
      synced: true, mode, verified_at: time,
      snapshot_uri: mode === "simulated" ? `workloom-sim://receipt/s${index + 1}` : `https://receipt.example/s${index + 1}`,
    },
  }))];
  return {
    dispatch: { kind: "routed", mode: "quest", threadId: "T-100", status: "completed", stepsDone: steps, stepsTotal: steps },
    thread: { id: "T-100", mode: "quest", status: "completed", progress_done: steps, progress_total: steps },
    apiEvents: events,
    ledger: events.map((payload) => ({ ...scope, session_id: "T-100", payload: structuredClone(payload) })),
    agents: [{ workspace_id: "ws-a", preset_key: "operator", status: "ready", readonly: false }],
    requests: [], queries: [], briefings: 0,
  };
}

beforeEach(() => {
  vi.resetModules();
  doubles.current = successfulScenario();
  vi.stubEnv("SERVER_BASE", "http://release-gate.test");
  vi.stubEnv("RELEASE_GATE_TOKEN", "test-only-token");
  vi.stubEnv("RELEASE_WORKSPACE_ID", "");
  vi.stubEnv("RELEASE_PRESET_KEY", "");
  vi.stubEnv("GATE_QUEST_TIMEOUT_MS", "100");
  vi.stubEnv("LLM_PROVIDER", "mock");
  vi.stubGlobal("fetch", vi.fn(async (raw: string | URL, options?: RequestInit) => {
    const state = doubles.current;
    const url = new URL(raw);
    if (url.pathname === "/health") return new Response("ok");
    const path = url.pathname.replace("/trpc/", "");
    const input = JSON.parse(options?.body ? String(options.body) : url.searchParams.get("input") ?? "{}") as Record<string, unknown>;
    state.requests.push({ path, input });
    if (path === state.failAt) return new Response(JSON.stringify({ error: { message: `${path}: FORBIDDEN` } }), { status: 403 });
    if (path === state.timeoutAt) {
      return new Promise<Response>((_resolve, reject) => {
        const abort = () => reject(new Error(`${path} timed out`));
        if (options?.signal?.aborted) abort();
        else options?.signal?.addEventListener("abort", abort, { once: true });
      });
    }
    let data: unknown;
    if (path === "threads.dispatch") {
      if (!input.runImmediately) data = { kind: "routed", mode: "ask", answer: "当前工作区存在可回读的关键进展与待办事项。", via: "fixture" };
      else if (state.timeoutDispatch) {
        return new Promise<Response>((_resolve, reject) => {
          const abort = () => reject(new Error("Quest request timed out"));
          if (options?.signal?.aborted) abort();
          else options?.signal?.addEventListener("abort", abort, { once: true });
        });
      } else data = state.dispatch;
    } else if (path === "threads.get") data = state.thread;
    else if (path === "threads.events") data = state.apiEvents;
    else if (path === "captain.runBeat") { state.briefings += 1; data = { eventId: "E-200" }; }
    else throw new Error(`Unexpected HTTP request in isolated gate test: ${path}`);
    return new Response(JSON.stringify({ result: { data } }), { headers: { "content-type": "application/json" } });
  }));
});
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.restoreAllMocks(); });

class GateExit extends Error { constructor(readonly code: number) { super(`gate exit ${code}`); } }
async function runGate() {
  const lines: string[] = [];
  vi.spyOn(console, "log").mockImplementation((...values) => { lines.push(values.join(" ")); });
  vi.spyOn(process, "exit").mockImplementation((code) => { throw new GateExit(Number(code ?? 0)); });
  let code = 0;
  try { await import("./release-gate.js"); }
  catch (error) {
    if (!(error instanceof GateExit)) throw error;
    code = error.code;
  }
  return { code, log: lines.join("\n") };
}
async function expectBlocked(id: "Q-01" | "Q-02") {
  const result = await runGate();
  expect(result.code, result.log).toBe(1);
  expect(result.log).toMatch(new RegExp(`✗ ${id} `));
  expect(result.log).not.toContain("准许发布");
  return result;
}
function apiEvents(): Payload[] { return doubles.current.apiEvents as Payload[]; }

describe("release gate: completed Quest evidence", () => {
  it.each([2, 6])("valid %i-step completion keeps all original 11 checks", async (steps) => {
    doubles.current = successfulScenario(steps);
    const result = await runGate();
    expect(result.code, result.log).toBe(0);
    expect(result.log).toContain("11/11 通过");
    for (const id of ["G-B0", "G-B1", "G-00", "G-01", "A-01", "A-02", "Q-01", "Q-02", "T-01", "T-02", "T-03"]) {
      expect(result.log).toContain(`✓ ${id} `);
    }
  });
  it("reports simulated receipt mode explicitly", async () => {
    const result = await runGate();
    expect(result.code, result.log).toBe(0);
    expect(result.log).toMatch(/Q-01.*simulated/);
  });
  it("accepts verified real receipts and reports their mode", async () => {
    doubles.current = successfulScenario(2, "real");
    const result = await runGate();
    expect(result.code, result.log).toBe(0);
    expect(result.log).toMatch(/Q-01.*real/);
  });
  it("regression: failed 0/2 Quest with a readable thread cannot permit release", async () => {
    Object.assign(doubles.current.dispatch!, { status: "failed", stepsDone: 0 });
    Object.assign(doubles.current.thread!, { status: "failed", progress_done: 0 });
    apiEvents().splice(2);
    apiEvents()[1]!.receipt = { synced: false, mode: "real" };
    await expectBlocked("Q-01");
  });
  it.each(["pending_review", "paused", "queued", "running", "partial", "failed"])("dispatch status %s is not completed", async (status) => {
    doubles.current.dispatch!.status = status;
    await expectBlocked("Q-01");
  });
  it.each(["pending_review", "paused", "running", "failed"])("stored thread status %s cannot be hidden by the dispatch response", async (status) => {
    doubles.current.thread!.status = status;
    await expectBlocked("Q-01");
  });
  it.each([0, 1, -1, 1.5, "2", null])("dispatch done=%s is not complete integer progress", async (stepsDone) => {
    doubles.current.dispatch!.stepsDone = stepsDone;
    await expectBlocked("Q-01");
  });
  it.each([0, 1, -1, 2.5, "2", null])("dispatch total=%s does not prove a valid multi-step plan", async (stepsTotal) => {
    doubles.current.dispatch!.stepsTotal = stepsTotal;
    await expectBlocked("Q-01");
  });
  it.each([
    { progress_done: 0 }, { progress_done: 1 }, { progress_done: 3 }, { progress_done: "2" },
    { progress_total: 0 }, { progress_total: 3 }, { progress_total: "2" }, { mode: "ask" }, { id: "T-other" },
  ])("stored thread %j must match the completed dispatch", async (change) => {
    Object.assign(doubles.current.thread!, change);
    await expectBlocked("Q-01");
  });
  it("missing/inaccessible thread fails closed", async () => {
    doubles.current.thread = null;
    await expectBlocked("Q-01");
  });
  it.each([null, { kind: "clarify" }, { kind: "routed", mode: "ask" }])("invalid Quest dispatch %j fails closed", async (dispatch) => {
    doubles.current.dispatch = dispatch;
    await expectBlocked("Q-01");
  });
});

describe("release gate: receipts and attribution", () => {
  it.each([[], null, {}])("empty or malformed event stream %j fails closed", async (events) => {
    doubles.current.apiEvents = events;
    await expectBlocked("Q-01");
  });
  it("dispatch alone cannot masquerade as executed steps", async () => {
    doubles.current.apiEvents = apiEvents().slice(0, 1);
    await expectBlocked("Q-01");
  });
  it.each([
    undefined, {}, { synced: false }, { synced: "true" },
    { synced: true, mode: "real" },
    { synced: true, mode: "simulated", verified_at: "bad-date", snapshot_uri: "workloom-sim://receipt/1" },
    { synced: true, mode: "simulated", verified_at: "2026-10-20T00:00:00Z", snapshot_uri: " " },
  ])("invalid/unverified receipt %j cannot prove completion", async (receipt) => {
    apiEvents()[1]!.receipt = receipt;
    await expectBlocked("Q-01");
  });
  it("execute error is rejected even if its receipt claims synced=true", async () => {
    apiEvents()[1]!.decision.outcome = "error";
    await expectBlocked("Q-01");
  });
  it("receipt error is rejected even if synced=true", async () => {
    apiEvents()[1]!.receipt!.error = "connector failed";
    await expectBlocked("Q-01");
  });
  it.each(["review", "block"])("%s events are not executed receipts", async (kind) => {
    apiEvents()[1]!.decision.kind = kind;
    await expectBlocked("Q-01");
  });
  it("two copies of one step do not replace a missing step", async () => {
    apiEvents()[2]!.decision.step_id = "s1";
    await expectBlocked("Q-01");
  });
  it("repeated event identity is rejected", async () => {
    apiEvents()[2]!.event_id = apiEvents()[1]!.event_id;
    await expectBlocked("Q-01");
  });
  it.each(["tenant_id", "workspace_id"] as const)("events from another %s cannot prove this Quest", async (field) => {
    apiEvents()[1]!.context[field] = "other";
    await expectBlocked("Q-01");
  });
  it("wrong-thread dispatch evidence cannot prove this Quest", async () => {
    apiEvents()[0]!.object.id = "T-other";
    await expectBlocked("Q-01");
  });
  it("another member's dispatch cannot prove this gate run", async () => {
    apiEvents()[0]!.who.id = "MEM-other";
    await expectBlocked("Q-01");
  });
  it("Q02 must not accept an unrelated recent ASK session", async () => {
    doubles.current.ledger = [{ ...scope, session_id: "T-unrelated-ask", payload: {
      ...structuredClone(apiEvents()[0]!), object: { type: "thread", id: "T-unrelated-ask" },
      decision: { action: "thread.dispatch", after: { mode: "ask", threadId: "T-unrelated-ask" } },
    } }];
    await expectBlocked("Q-02");
  });
  it.each(["tenant_id", "workspace_id", "session_id"] as const)("Q02 binds ledger %s to Q01", async (field) => {
    doubles.current.ledger.forEach((row) => { row[field] = "other"; });
    await expectBlocked("Q-02");
  });
  it("Q02 rejects a same-session stream with no execute evidence", async () => {
    doubles.current.ledger = doubles.current.ledger.slice(0, 1);
    await expectBlocked("Q-02");
  });
  it("Q02 rejects different execution event IDs from the API receipt", async () => {
    doubles.current.ledger[1]!.payload.event_id = "E-999";
    await expectBlocked("Q-02");
  });
  it("Q02 rejects changed receipt content even when event IDs match", async () => {
    doubles.current.ledger[1]!.payload.receipt!.snapshot_uri = "workloom-sim://receipt/different";
    await expectBlocked("Q-02");
  });
  it("Q02 compares event content independently of JSON key insertion order", async () => {
    const first = doubles.current.ledger[1]!.payload.receipt!;
    doubles.current.ledger[1]!.payload.receipt = Object.fromEntries(Object.entries(first).reverse());
    const result = await runGate();
    expect(result.code, result.log).toBe(0);
  });
  it("Q02 cannot pass after Q01 fails even if old successful events exist", async () => {
    doubles.current.dispatch!.status = "failed";
    await expectBlocked("Q-02");
  });
  it("Q02 uses explicit tenant/workspace/session predicates and scoped credentials", async () => {
    const result = await runGate();
    expect(result.code, result.log).toBe(0);
    const query = doubles.current.queries.find(({ sql }) => sql.includes("FROM biz_events") && !sql.includes("ceo.briefing"))!;
    expect(query.sql).toMatch(/tenant_id\s*=\s*\$1/);
    expect(query.sql).toMatch(/workspace_id\s*=\s*\$2/);
    expect(query.sql).toMatch(/session_id\s*=\s*\$3/);
    expect(query.params).toEqual(["tenant-a", "ws-a", "T-100"]);
    expect(query.tenant).toBe("tenant-a");
    expect(query.workspace).toBe("ws-a");
  });
});

describe("release gate: unavailable evidence is a failure", () => {
  it.each(["threads.dispatch", "threads.get", "threads.events"] as const)("HTTP %s permission failure blocks release", async (failAt) => {
    doubles.current.failAt = failAt;
    await expectBlocked("Q-01");
  });
  it("ledger permission failure blocks Q02", async () => {
    doubles.current.failAt = "ledger";
    await expectBlocked("Q-02");
  });
  it("Quest request timeout blocks release without marking completion", async () => {
    doubles.current.timeoutDispatch = true;
    const result = await expectBlocked("Q-01");
    expect(result.log).toContain("Quest request timed out");
  });
  it.each(["threads.get", "threads.events"] as const)("%s evidence read has a bounded timeout", async (timeoutAt) => {
    doubles.current.timeoutAt = timeoutAt;
    const result = await expectBlocked("Q-01");
    expect(result.log).toContain(`${timeoutAt} timed out`);
  });
  it.each(["0", "-1", "NaN", "Infinity", "2147483648"])("invalid timeout %s fails before Quest dispatch", async (timeout) => {
    vi.stubEnv("GATE_QUEST_TIMEOUT_MS", timeout);
    const result = await expectBlocked("Q-01");
    expect(result.log).toContain("GATE_QUEST_TIMEOUT_MS");
    expect(doubles.current.requests.some(({ input }) => input.runImmediately === true)).toBe(false);
  });
});

describe("release gate: explicit installed preset selection", () => {
  const analyst: InstalledAgent = { workspace_id: "ws-a", preset_key: "attribution-analyst", status: "ready", readonly: true };
  function installAnalyst(change: Partial<InstalledAgent> = {}) {
    doubles.current.agents.push({ ...analyst, ...change });
  }
  function useAnalystEvidence() {
    for (const event of apiEvents()) if (event.who.type === "agent") event.who.id = analyst.preset_key;
    doubles.current.ledger = apiEvents().map((payload) => ({ ...scope, session_id: "T-100", payload: structuredClone(payload) }));
  }
  async function expectUnavailable() {
    const result = await runGate();
    expect(result.code, result.log).toBe(1);
    expect(result.log).toContain("✗ G-01");
    expect(result.log).not.toContain("准许发布");
    expect(doubles.current.requests.filter(({ path }) => path === "threads.dispatch")).toEqual([]);
    expect(doubles.current.queries.filter(({ sql }) => sql.includes("FROM workspaces w"))).toHaveLength(1);
    return result;
  }

  it.each(["", " \t "])("empty preset option %j keeps the default writable-only selection", async (value) => {
    installAnalyst();
    vi.stubEnv("RELEASE_PRESET_KEY", value);
    const result = await runGate();
    expect(result.code, result.log).toBe(0);
    expect(doubles.current.requests.filter(({ path }) => path === "threads.dispatch").every(({ input }) => input.presetKey === "operator")).toBe(true);
    expect(result.log).toContain("readonly=false");
    expect(result.log).toContain("默认非只读岗位");
  });

  it("default discovery cannot silently use a readonly-only workspace", async () => {
    doubles.current.agents = [{ ...analyst }];
    await expectUnavailable();
  });

  it.each(["ready", "active"])("explicit installed readonly preset in state %s is selected exactly", async (status) => {
    installAnalyst({ status });
    useAnalystEvidence();
    vi.stubEnv("RELEASE_WORKSPACE_ID", "ws-a");
    vi.stubEnv("RELEASE_PRESET_KEY", analyst.preset_key);
    const result = await runGate();
    expect(result.code, result.log).toBe(0);
    expect(result.log).toContain("11/11 通过");
    expect(doubles.current.requests.filter(({ path }) => path === "threads.dispatch").every(({ input }) => input.presetKey === analyst.preset_key)).toBe(true);
    expect(result.log).toContain("RELEASE_PRESET_KEY");
    expect(result.log).toContain(analyst.preset_key);
    expect(result.log).toContain("readonly=true");
    expect(result.log).toContain("只读核心结构正例，不证明写任务或生产能力");
    const query = doubles.current.queries.find(({ sql }) => sql.includes("FROM workspaces w"))!;
    expect(query.params).toEqual(["ws-a", analyst.preset_key]);
    expect(query.sql).toMatch(/workspace_id\s*=\s*w\.id/);
    expect(query.sql).toMatch(/preset_key\s*=\s*\$2/);
    expect(query.sql).toMatch(/status\s+IN\s*\('ready','active'\)/);
  });

  it("an explicit readonly preset does not require a separate writable preset", async () => {
    doubles.current.agents = [{ ...analyst }];
    useAnalystEvidence();
    vi.stubEnv("RELEASE_PRESET_KEY", analyst.preset_key);
    const result = await runGate();
    expect(result.code, result.log).toBe(0);
    expect(doubles.current.requests.some(({ input }) => input.runImmediately && input.presetKey === analyst.preset_key)).toBe(true);
  });

  it("an explicit writable preset remains available with truthful scope", async () => {
    vi.stubEnv("RELEASE_PRESET_KEY", "operator");
    const result = await runGate();
    expect(result.code, result.log).toBe(0);
    expect(result.log).toContain("RELEASE_PRESET_KEY");
    expect(result.log).toContain("readonly=false");
    expect(result.log).not.toContain("只读核心结构正例");
  });

  it("explicit preset and workspace options are trimmed before exact parameter binding", async () => {
    installAnalyst();
    useAnalystEvidence();
    vi.stubEnv("RELEASE_WORKSPACE_ID", " ws-a ");
    vi.stubEnv("RELEASE_PRESET_KEY", " attribution-analyst ");
    const result = await runGate();
    expect(result.code, result.log).toBe(0);
    const query = doubles.current.queries.find(({ sql }) => sql.includes("FROM workspaces w"))!;
    expect(query.params).toEqual(["ws-a", "attribution-analyst"]);
  });

  it.each(["missing-preset", "Attribution-Analyst"])("unavailable explicit preset %s does not fall back to the default operator", async (key) => {
    installAnalyst();
    vi.stubEnv("RELEASE_PRESET_KEY", key);
    const result = await expectUnavailable();
    expect(result.log).toContain("RELEASE_PRESET_KEY");
    expect(result.log).toContain(key);
    expect(result.log).toContain("拒绝回退");
  });

  it.each(["disabled", "invalid", "queued"])("explicit installed preset in state %s cannot fall back to an enabled one", async (status) => {
    installAnalyst({ status });
    vi.stubEnv("RELEASE_PRESET_KEY", analyst.preset_key);
    await expectUnavailable();
  });

  it("an identically named preset in another workspace cannot satisfy the explicit target", async () => {
    installAnalyst({ workspace_id: "ws-b" });
    vi.stubEnv("RELEASE_WORKSPACE_ID", "ws-a");
    vi.stubEnv("RELEASE_PRESET_KEY", analyst.preset_key);
    await expectUnavailable();
  });

  it("an unavailable requested workspace cannot fall back to the detected workspace", async () => {
    installAnalyst();
    vi.stubEnv("RELEASE_WORKSPACE_ID", "ws-b");
    vi.stubEnv("RELEASE_PRESET_KEY", analyst.preset_key);
    await expectUnavailable();
  });

  it("special characters in an unavailable preset remain a bound value, not SQL", async () => {
    const key = "analyst' OR true --";
    vi.stubEnv("RELEASE_PRESET_KEY", key);
    await expectUnavailable();
    const query = doubles.current.queries.find(({ sql }) => sql.includes("FROM workspaces w"))!;
    expect(query.params).toEqual([null, key]);
    expect(query.sql).not.toContain(key);
  });

  it.each(["failed", "pending_review"])("selecting readonly does not waive the completed requirement for %s", async (status) => {
    installAnalyst();
    useAnalystEvidence();
    vi.stubEnv("RELEASE_PRESET_KEY", analyst.preset_key);
    doubles.current.dispatch!.status = status;
    await expectBlocked("Q-01");
    expect(doubles.current.requests.some(({ input }) => input.runImmediately && input.presetKey === analyst.preset_key)).toBe(true);
  });
});
