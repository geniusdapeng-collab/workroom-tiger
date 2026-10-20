import assert from "node:assert/strict";
import { copyFileSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  bindUiLockfileIntegrity,
  REQUIRED_CLIENT_PACKAGES,
  uiStateLockContractErrors,
} from "../governance/sync/ui-lockfile-integrity.mjs";

const repo = dirname(dirname(fileURLToPath(import.meta.url)));
const governance = join(repo, "governance");
const readJson = (path) => JSON.parse(readFileSync(join(governance, path), "utf8"));
const state = readJson(".workloom-ui.json");
const selector = `${state.package}@${state.version}`;
const expectedOverride = [[selector, state.artifact.url]];

function uiOverrides(overrides) {
  return Object.entries(overrides).filter(([key]) => {
    const leaf = key.split(">").at(-1);
    return leaf === state.package || leaf.startsWith(`${state.package}@`);
  });
}

test("shared UI state identifies one approved immutable artifact", () => {
  assert.deepEqual(uiStateLockContractErrors(state), []);
});

test("all required clients consume the approved UI version", async (t) => {
  for (const path of REQUIRED_CLIENT_PACKAGES) {
    await t.test(path, () => {
      assert.equal(readJson(path).dependencies[state.package], state.version);
    });
  }
});

test("package manifest UI override agrees with the approved artifact", () => {
  const overrides = readJson("package.json").pnpm?.overrides;
  assert.ok(overrides && typeof overrides === "object");
  assert.deepEqual(uiOverrides(overrides), expectedOverride);
});

test("workspace UI override agrees with the manifest and frozen lockfile", () => {
  const source = readFileSync(join(governance, "pnpm-workspace.yaml"), "utf8");
  const blocks = [...source.matchAll(/^overrides:\r?\n((?:[ \t].*\r?\n|\r?\n)*)/gm)];
  assert.equal(blocks.length, 1, "workspace must contain exactly one overrides mapping");
  // The checked-in workspace uses JSON-quoted string pairs. Keep this assertion
  // dependency-free so it can diagnose drift before pnpm can install YAML.
  const entries = blocks[0][1].split(/\r?\n/u)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .flatMap((line) => Object.entries(JSON.parse(`{${line}}`)));
  assert.equal(new Set(entries.map(([key]) => key)).size, entries.length,
    "workspace must not contain duplicate override selectors");
  assert.deepEqual(uiOverrides(Object.fromEntries(entries)), expectedOverride);
});

test("frozen UI lockfile already carries the approved URL and SHA-512", (t) => {
  const fixture = mkdtempSync(join(tmpdir(), "tiger-install-contract-"));
  t.after(() => rmSync(fixture, { recursive: true, force: true }));
  copyFileSync(join(governance, "pnpm-lock.yaml"), join(fixture, "pnpm-lock.yaml"));
  const result = bindUiLockfileIntegrity(fixture, state);
  assert.equal(result.changed, false, "install must not need to repair the frozen lockfile");
  assert.equal(result.integrity, state.artifact.sha512);
  assert.equal(result.packageKey, `${state.package}@${state.artifact.url}`);
});
