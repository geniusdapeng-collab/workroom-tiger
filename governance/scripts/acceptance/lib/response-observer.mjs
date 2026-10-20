/**
 * Observe the browser exchange belonging to a journey, without mutating or
 * cancelling product requests. An unfinished Response.json() must not prevent
 * that journey's finally block from returning and writing its evidence.
 */
import { rpcExchange } from "./ui-evidence.mjs";

const BODY_TIMEOUT_MS = 8000;
const now = () => Date.now();
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function requestDetails(request) {
  const url = request.url();
  let parsed, names;
  try {
    parsed = new URL(url);
    const marker = parsed.pathname.indexOf("/trpc/");
    names = marker >= 0 ? decodeURIComponent(parsed.pathname.slice(marker + 6)).split(",")
      : parsed.pathname === "/c/chat" ? ["c.chat"] : [];
  } catch {
    // A malformed/non-RPC URL cannot be business evidence.
    return null;
  }
  if (!names.length) return null;
  let input;
  try { input = request.postDataJSON(); }
  catch { input = null; }
  return { url, names, input, method: request.method(), requestAt: now(), settled: false, status: null };
}

export function observeResponses(target) {
  const responses = [];
  const requests = [];
  const failures = [];
  const owned = new WeakMap();
  const pending = new Set();
  let stopped = false;
  let closed = false;
  let stopPromise;

  const finish = (entry, json, readError = null) => {
    if (entry.settled) return;
    entry.settled = true;
    clearTimeout(entry.timer);
    const at = now();
    for (const procedure of entry.names) {
      const exchange = procedure === "c.chat"
        ? { procedure, input: entry.input, data: json, status: entry.status,
          ok: entry.status >= 200 && entry.status < 300 && typeof json?.answer === "string" && Boolean(json.answer.trim()) }
        : rpcExchange(entry.url, entry.method, entry.input, entry.status, json, procedure);
      if (!exchange) continue;
      const row = {
        ...exchange, method: entry.method, requestAt: entry.requestAt, at,
        ...(readError ? { ok: false, data: null, readError } : {}),
      };
      responses.push(row);
      if (readError) {
        // Retain an actionable code and timing, never bodies, inputs, headers,
        // URL queries or transport error text that might contain credentials.
        failures.push({ procedure, method: entry.method, status: entry.status,
          requestAt: entry.requestAt, at, ok: false, readError });
      }
    }
    pending.delete(entry);
    entry.resolve?.();
  };

  const onRequest = (request) => {
    if (stopped || closed || owned.has(request)) return;
    const entry = requestDetails(request);
    if (!entry) return;
    owned.set(request, entry);
    if (entry.method === "POST") {
      for (const procedure of entry.names) requests.push({ procedure, at: entry.requestAt });
    }
  };
  const onResponse = (response) => {
    if (stopped || closed) return;
    const entry = owned.get(response.request());
    // A response from a request started before this observer is stale evidence.
    if (!entry || entry.settled || entry.done) return;
    entry.status = response.status();
    entry.done = new Promise((resolve) => { entry.resolve = resolve; });
    pending.add(entry);
    entry.timer = setTimeout(() => finish(entry, null, {
      code: "body-timeout", timeoutMs: BODY_TIMEOUT_MS,
    }), BODY_TIMEOUT_MS);
    const failed = (error) => finish(entry, null, {
      code: error?.name === "SyntaxError" ? "invalid-json" : "body-read-failed",
    });
    try {
      // Attach both continuations immediately. A late resolve/reject is consumed
      // but cannot alter this or the next journey after terminal settlement.
      Promise.resolve(response.json()).then((json) => finish(entry, json), failed);
    } catch (error) {
      failed(error);
    }
  };
  const onRequestFailed = (request) => {
    const entry = owned.get(request);
    if (entry) finish(entry, null, { code: "request-failed" });
  };
  const detachInput = () => {
    target.off("request", onRequest);
    target.off("response", onResponse);
  };
  const onClose = () => {
    closed = true;
    detachInput();
    for (const entry of [...pending]) finish(entry, null, { code: "page-closed" });
  };
  target.on("request", onRequest);
  target.on("response", onResponse);
  target.on("requestfailed", onRequestFailed);
  target.on("close", onClose);

  const latest = (name, match = () => true) => responses.findLast((entry) => entry.procedure === name && match(entry)) ?? null;
  return {
    responses, requests, failures, latest,
    async wait(name, match = () => true, timeout = 8000) {
      const end = now() + timeout;
      do {
        const found = latest(name, match);
        if (found || closed || stopped) return found;
        await sleep(100);
      } while (now() < end);
      return latest(name, match);
    },
    stop() {
      if (stopPromise) return stopPromise;
      stopped = true;
      detachInput();
      // Each pending read retains its own original deadline; stop never starts
      // another budget or waits for the underlying unbounded Playwright call.
      stopPromise = Promise.all([...pending].map((entry) => entry.done)).then(() => {
        target.off("requestfailed", onRequestFailed);
        target.off("close", onClose);
      });
      return stopPromise;
    },
  };
}
