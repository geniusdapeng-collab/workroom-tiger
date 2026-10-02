#!/usr/bin/env node
/** Newline-delimited stdio MCP; all stdout is JSON-RPC. */
import { getArtifacts, getJob, launcherOptions, MAX_FRAME_BYTES, normalizeRequest,
  runRequest, safeError, TigerAgentError, TOOLS, validateObject } from "./tiger-agent-runtime.mjs";

const PROTOCOLS = ["2025-11-25", "2025-06-18", "2024-11-05"];
const requests = new Map();
const jobs = new Map();
let initialized = false;
let ready = false;
let closed = false;
let buffer = Buffer.alloc(0);
let dropping = false;
let options;
try {
  const parsed = launcherOptions(process.argv.slice(2));
  options = parsed.options;
  if (parsed.rest.length) throw new TigerAgentError("INVALID_ARGUMENTS", "Unexpected MCP launcher arguments");
} catch (error) {
  process.stderr.write(JSON.stringify(safeError(error)) + "\n");
  process.exit(1);
}

function send(value) {
  if (!closed && !process.stdout.destroyed) process.stdout.write(JSON.stringify(value) + "\n");
}
function rpcError(id, code, message) { send({ jsonrpc: "2.0", id, error: { code, message } }); }
const idValid = (id) => typeof id === "string" || (typeof id === "number" && Number.isSafeInteger(id));

async function toolCall(params, task) {
  validateObject(params, ["name", "arguments", "_meta"], ["name"]);
  if (!TOOLS.some((tool) => tool.name === params.name)) throw new TigerAgentError("UNKNOWN_TOOL", "Unknown Tiger tool");
  const input = params.arguments ?? {};
  if (["tiger.pipeline.run", "tiger.employee.run"].includes(params.name)) {
    // operation is chosen by the tool, never by an input property.
    if (input && Object.hasOwn(input, "operation")) throw new TigerAgentError("INVALID_INPUT", "operation is selected by the tool name");
    const request = normalizeRequest({ ...input, operation: params.name === "tiger.pipeline.run" ? "pipeline" : "employee" });
    if (jobs.has(request.idempotencyKey)) {
      const running = jobs.get(request.idempotencyKey);
      throw new TigerAgentError(JSON.stringify(running.request) === JSON.stringify(request) ? "IN_PROGRESS" : "IDEMPOTENCY_CONFLICT",
        "Idempotency key already has an active request");
    }
    if (jobs.size >= 2) throw new TigerAgentError("BUSY", "This launcher already has two active kernel jobs");
    const job = { request, controller: task.controller, promise: null };
    jobs.set(request.idempotencyKey, job);
    try {
      job.promise = runRequest(options, request, { signal: task.controller.signal });
      return await job.promise;
    } finally { jobs.delete(request.idempotencyKey); }
  }
  if (params.name === "tiger.job.get") return getJob(options, input);
  if (params.name === "tiger.job.artifacts") return getArtifacts(options, input);
  validateObject(input, ["jobId"], ["jobId"]);
  const job = jobs.get(input.jobId);
  if (!job) {
    const receipt = await getJob(options, input);
    if (receipt.status === "running") throw new TigerAgentError("NOT_OWNED", "Job is running in another launcher; this server cannot cancel it");
    return receipt;
  }
  job.controller.abort();
  return await job.promise;
}

function receive(message) {
  if (!message || typeof message !== "object" || Array.isArray(message)
      || message.jsonrpc !== "2.0" || typeof message.method !== "string"
      || (Object.hasOwn(message, "id") && !idValid(message.id))) {
    rpcError(idValid(message?.id) ? message.id : null, -32600, "Invalid JSON-RPC request");
    return;
  }
  const hasId = Object.hasOwn(message, "id");
  if (!hasId) {
    if (message.method === "notifications/initialized" && initialized) ready = true;
    if (message.method === "notifications/cancelled") {
      const task = requests.get(message.params?.requestId);
      if (task && task.method !== "initialize") { task.suppress = true; task.controller.abort(); }
    }
    return;
  }
  const id = message.id;
  if (requests.has(id)) { rpcError(id, -32600, "Duplicate active request ID"); return; }
  if (message.method === "ping") { send({ jsonrpc: "2.0", id, result: {} }); return; }
  if (message.method === "initialize") {
    if (initialized || !message.params || typeof message.params.protocolVersion !== "string"
        || !message.params.clientInfo || typeof message.params.clientInfo.name !== "string"
        || typeof message.params.clientInfo.version !== "string"
        || !message.params.capabilities || typeof message.params.capabilities !== "object") {
      rpcError(id, -32602, "Invalid or repeated initialize"); return;
    }
    initialized = true;
    send({ jsonrpc: "2.0", id, result: { protocolVersion: PROTOCOLS.includes(message.params.protocolVersion)
      ? message.params.protocolVersion : PROTOCOLS[0], capabilities: { tools: { listChanged: false } },
      serverInfo: { name: "workroom-tiger-research", version: "1.0.0" },
      instructions: "Research and simulation only. Require an explicit environment and idempotency key. Degraded receipts disclose missing model output. Local hash verification is not governance server synchronization." } });
    return;
  }
  if (!ready) { rpcError(id, -32002, "Initialize and send notifications/initialized first"); return; }
  if (message.method === "tools/list") {
    if (message.params?.cursor !== undefined) { rpcError(id, -32602, "This fixed catalog has no cursor"); return; }
    send({ jsonrpc: "2.0", id, result: { tools: TOOLS } }); return;
  }
  if (message.method !== "tools/call") { rpcError(id, -32601, "Method not found"); return; }
  if (requests.size >= 8) { rpcError(id, -32000, "Too many concurrent tool requests"); return; }
  const task = { controller: new AbortController(), suppress: false, method: message.method, promise: null };
  requests.set(id, task);
  task.promise = (async () => {
    try {
      const result = await toolCall(message.params, task);
      if (!task.suppress) send({ jsonrpc: "2.0", id, result: {
        content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result,
        isError: !["succeeded", "degraded", "running"].includes(result.status)
          && !(message.params.name === "tiger.job.cancel" && result.status === "cancelled"),
      } });
    } catch (error) {
      if (!task.suppress) {
        if (error.code === "UNKNOWN_TOOL") rpcError(id, -32602, "Unknown Tiger tool");
        else {
          const failure = safeError(error);
          send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: JSON.stringify(failure) }],
            structuredContent: failure, isError: true } });
        }
      }
    } finally { requests.delete(id); }
  })();
}

function frame(bytes) {
  if (bytes.length > MAX_FRAME_BYTES) { rpcError(null, -32700, "MCP frame exceeds 128 KiB"); return; }
  try { receive(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes))); }
  catch { rpcError(null, -32700, "Invalid UTF-8 JSON frame"); }
}

process.stdin.on("data", (chunk) => {
  if (closed) return;
  buffer = Buffer.concat([buffer, chunk]);
  let newline;
  while ((newline = buffer.indexOf(10)) !== -1) {
    const bytes = buffer.subarray(0, newline);
    buffer = buffer.subarray(newline + 1);
    if (!dropping && bytes.length) frame(bytes);
    dropping = false;
  }
  if (buffer.length > MAX_FRAME_BYTES) {
    if (!dropping) rpcError(null, -32700, "MCP frame exceeds 128 KiB");
    dropping = true;
    buffer = Buffer.alloc(0);
  }
});
process.stdin.on("end", async () => {
  if (buffer.length && !dropping) frame(buffer);
  // EOF does not cancel valid work already accepted by this stdio session.
  await Promise.allSettled([...requests.values()].map((task) => task.promise));
});
async function shutdown(exitCode) {
  if (closed) return;
  closed = true;
  for (const task of requests.values()) { task.suppress = true; task.controller.abort(); }
  process.stdin.destroy();
  await Promise.allSettled([...requests.values()].map((task) => task.promise));
  process.exitCode = exitCode;
}
process.on("SIGINT", () => { void shutdown(130); });
process.on("SIGTERM", () => { void shutdown(143); });
process.stdout.on("error", () => { void shutdown(1); });
process.stdin.on("error", () => { void shutdown(1); });
