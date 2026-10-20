import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { verifyTigerClientRepairs } from "../scripts/ci/verify-tiger-client-repairs.mjs";

const repo = dirname(dirname(fileURLToPath(import.meta.url)));
const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
const star = "apps/web/src/components/star-ring/StarRing.tsx";
const api = "apps/webc/src/lib/api.ts";
const regression = "apps/web/src/components/star-ring/StarRing.contract.test.tsx";
const manifestFile = ".workloom-client-repairs.json";
const write = (root, path, content) => { mkdirSync(dirname(join(root, path)), { recursive: true }); writeFileSync(join(root, path), content); };

function fixture(t, repaired = true) {
  const root = mkdtempSync(join(tmpdir(), "tiger-client-repairs-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const g = join(root, "governance");
  const entries = ["apps/web/src/main.tsx", "apps/webb/src/main.tsx", "apps/webc/src/main.tsx", "apps/webc/public/service-front.config.json", star, api];
  for (const path of entries) write(g, path, `original:${path}\n`);
  write(g, ".workloom-ui.json", JSON.stringify({ version: "0.1.17" }));
  const state = {
    schemaVersion: "workloom.client-foundation-state/v1", version: "0.1.17",
    sourceCommit: "91610daf0962a6c24bc7b8cde6c802662eb8b2f7", sourceRef: "refs/tags/ui-v0.1.17", updatePolicy: "upgrade-pr-only",
    clients: { bPc: "apps/web", bMobile: "apps/webb", cMobile: "apps/webc" },
    allowedIndustryExtensionPaths: ["apps/*/src/extensions/**", "apps/*/src/projections/**", "apps/*/src/config/industry/**", "apps/*/src/theme/industry/**", "apps/*/public/industry/**"],
    managedFiles: Object.fromEntries(entries.map((path) => [path, { sha256: digest(readFileSync(join(g, path))), mode: 420 }])),
  };
  write(g, ".workloom-client-foundation.json", JSON.stringify(state));
  write(g, "scripts/verify-client-foundation-consumer.mjs", readFileSync(join(repo, "governance/scripts/verify-client-foundation-consumer.mjs")));
  const manifest = {
    schemaVersion: "workloom.tiger-client-repairs/v1", repository: "geniusdapeng-collab/workroom-tiger", taskId: "T-2026-1020-0901",
    authorization: { basis: "user-requested-repository-repair", issue: "https://github.com/geniusdapeng-collab/workroom-tiger/issues/24", pullRequest: "https://github.com/geniusdapeng-collab/workroom-tiger/pull/25" },
    base: { version: state.version, sourceCommit: state.sourceCommit, stateSha256: digest(readFileSync(join(g, ".workloom-client-foundation.json"))), verifierSha256: digest(readFileSync(join(g, "scripts/verify-client-foundation-consumer.mjs"))) },
    repairs: [], testAdditions: [],
  };
  if (repaired) {
    for (const [path, card] of [[star, "MC-012"], [api, "MC-018"]]) {
      write(g, path, `reviewed repair:${path}\n`);
      manifest.repairs.push({ path, card, upstreamSha256: state.managedFiles[path].sha256, repairedSha256: digest(readFileSync(join(g, path))), sourceCommit: "a".repeat(40), reason: "Reviewed contract repair with focused behavioral regression coverage." });
    }
    write(g, regression, "test('real component contract', () => {});\n");
    manifest.testAdditions.push({ path: regression, card: "MC-012", sha256: digest(readFileSync(join(g, regression))) });
    write(g, manifestFile, JSON.stringify(manifest));
  }
  return { root, g, manifest, state, save: () => write(g, manifestFile, JSON.stringify(manifest)) };
}

test("an unchanged upstream consumer retains the strict gate without a local manifest", (t) => {
  assert.deepEqual(verifyTigerClientRepairs(fixture(t, false).root), []);
});
test("exact reviewed repairs and the bound regression file pass without rewriting upstream state", (t) => {
  const f = fixture(t); const before = readFileSync(join(f.g, ".workloom-client-foundation.json"));
  assert.deepEqual(verifyTigerClientRepairs(f.root), []);
  assert.deepEqual(readFileSync(join(f.g, ".workloom-client-foundation.json")), before);
});
test("a repair manifest is required for modified managed sources", (t) => {
  const f = fixture(t); rmSync(join(f.g, manifestFile));
  assert.ok(verifyTigerClientRepairs(f.root).some((e) => e.includes(star)));
});
test("an unregistered managed source change still fails", (t) => {
  const f = fixture(t); write(f.g, "apps/web/src/main.tsx", "unreviewed\n");
  assert.ok(verifyTigerClientRepairs(f.root).some((e) => e.includes("apps/web/src/main.tsx")));
});
test("changing a registered repair after review fails its exact hash", (t) => {
  const f = fixture(t); write(f.g, star, "unreviewed follow-up\n");
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
});
test("changing or omitting the bound regression file fails", (t) => {
  const f = fixture(t); write(f.g, regression, "different test\n");
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
  rmSync(join(f.g, regression));
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
});
test("a newly added source outside the fixed repair set still fails", (t) => {
  const f = fixture(t); write(f.g, "apps/web/src/unreviewed.ts", "export const bypass = true;\n");
  assert.ok(verifyTigerClientRepairs(f.root).some((e) => e.includes("unreviewed.ts")));
});
test("managed file mode checks remain active for reviewed repairs", (t) => {
  const f = fixture(t); chmodSync(join(f.g, star), 0o755);
  assert.ok(verifyTigerClientRepairs(f.root).some((e) => e.includes("模式")));
});
test("missing reviewed sources remain failures", (t) => {
  const f = fixture(t); rmSync(join(f.g, star));
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
});
test("a valid upstream version upgrade requires a new compatible repair registration", (t) => {
  const f = fixture(t); f.state.version = "0.1.18"; f.state.sourceRef = "refs/tags/ui-v0.1.18";
  write(f.g, ".workloom-client-foundation.json", JSON.stringify(f.state)); write(f.g, ".workloom-ui.json", JSON.stringify({ version: "0.1.18" }));
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
});
for (const field of ["stateSha256", "verifierSha256", "sourceCommit", "version"]) {
  test(`invalid base binding ${field} fails closed`, (t) => {
    const f = fixture(t); f.manifest.base[field] = "invalid"; f.save();
    assert.ok(verifyTigerClientRepairs(f.root).length > 0);
  });
}
for (const kind of ["unknown-path", "duplicate", "wrong-origin", "wrong-card", "bad-commit", "wrong-repository", "wrong-authorization", "traversal", "test-as-production"]) {
  test(`invalid repair registration ${kind} fails closed`, (t) => {
    const f = fixture(t);
    if (kind === "unknown-path") f.manifest.repairs[0].path = "apps/web/src/main.tsx";
    if (kind === "duplicate") f.manifest.repairs.push({ ...f.manifest.repairs[0] });
    if (kind === "wrong-origin") f.manifest.repairs[0].upstreamSha256 = "b".repeat(64);
    if (kind === "wrong-card") f.manifest.repairs[0].card = "MC-018";
    if (kind === "bad-commit") f.manifest.repairs[0].sourceCommit = "main";
    if (kind === "wrong-repository") f.manifest.repository = "other/project";
    if (kind === "wrong-authorization") f.manifest.authorization.basis = "inferred";
    if (kind === "traversal") f.manifest.repairs[0].path = "../outside";
    if (kind === "test-as-production") f.manifest.testAdditions[0].path = "apps/web/src/new-runtime.tsx";
    f.save(); assert.ok(verifyTigerClientRepairs(f.root).length > 0);
  });
}
test("a changed upstream verifier cannot supply a false clean result", (t) => {
  const f = fixture(t); write(f.g, "scripts/verify-client-foundation-consumer.mjs", "export const verifyClientFoundationConsumer = () => [];\n");
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
});
test("omitting the card cannot authorize an otherwise hash-matched unknown path", (t) => {
  const f = fixture(t, false);
  const path = "apps/web/src/main.tsx";
  write(f.g, path, "unapproved entry-point change\n");
  f.manifest.repairs = [{ path, upstreamSha256: f.state.managedFiles[path].sha256,
    repairedSha256: digest(readFileSync(join(f.g, path))), sourceCommit: "a".repeat(40),
    reason: "An unknown path must remain forbidden even when a card field is absent." }];
  f.save();
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
});
test("malformed JSON is reported as a validation failure", (t) => {
  const f = fixture(t); write(f.g, manifestFile, "{");
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
});
test("a symlink cannot substitute a reviewed source", (t) => {
  const f = fixture(t); copyFileSync(join(f.g, star), join(f.root, "outside.tsx")); rmSync(join(f.g, star)); symlinkSync(join(f.root, "outside.tsx"), join(f.g, star));
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
});
