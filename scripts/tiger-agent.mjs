#!/usr/bin/env node
/** Machine-readable CLI for the Tiger-owned research API. */
import { createReadStream } from "node:fs";
import { catalog, getArtifacts, getJob, launcherOptions, MAX_INPUT_BYTES, runRequest, safeError, TigerAgentError } from "./tiger-agent-runtime.mjs";

async function inputText(rest) {
  if (rest.length !== 2 || !["--json", "--input-file"].includes(rest[0])) {
    throw new TigerAgentError("INVALID_ARGUMENTS", "run requires --json JSON or --input-file FILE (use - for stdin)");
  }
  if (rest[0] === "--json") {
    if (Buffer.byteLength(rest[1]) > MAX_INPUT_BYTES) throw new TigerAgentError("INPUT_TOO_LARGE", "Request exceeds 64 KiB");
    return rest[1];
  }
  const stream = rest[1] === "-" ? process.stdin : createReadStream(rest[1]);
  let bytes = 0;
  const chunks = [];
  for await (const chunk of stream) {
    bytes += chunk.length;
    if (bytes > MAX_INPUT_BYTES) {
      stream.destroy();
      throw new TigerAgentError("INPUT_TOO_LARGE", "Request exceeds 64 KiB");
    }
    chunks.push(chunk);
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
}

const controller = new AbortController();
const interrupt = () => controller.abort();
process.on("SIGINT", interrupt);
process.on("SIGTERM", interrupt);
try {
  const { options, rest } = launcherOptions(process.argv.slice(2));
  const command = rest.shift();
  let output;
  if (command === "catalog" && rest.length === 0) output = catalog();
  else if (command === "run") {
    let input;
    try { input = JSON.parse(await inputText(rest)); }
    catch (error) {
      if (error instanceof TigerAgentError) throw error;
      throw new TigerAgentError("INVALID_INPUT", "Input must be UTF-8 JSON within the supported input limit");
    }
    output = await runRequest(options, input, { signal: controller.signal });
  } else if (command === "get" && rest.length === 1) output = await getJob(options, { jobId: rest[0] });
  else if (command === "artifacts" && [1, 2].includes(rest.length)) output = await getArtifacts(options,
    { jobId: rest[0], ...(rest[1] ? { name: rest[1] } : {}) });
  else throw new TigerAgentError("INVALID_ARGUMENTS", "Use catalog, run, get JOB_ID, or artifacts JOB_ID [NAME]; configure --workspace ABS explicitly");
  process.stdout.write(JSON.stringify(output) + "\n");
  process.exitCode = output.status === "degraded" ? 10
    : output.status && !["succeeded", "running"].includes(output.status) ? 1 : 0;
} catch (error) {
  process.stdout.write(JSON.stringify(safeError(error)) + "\n");
  process.exitCode = 1;
} finally {
  process.off("SIGINT", interrupt);
  process.off("SIGTERM", interrupt);
}
