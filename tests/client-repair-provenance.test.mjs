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
const consumer = "apps/web/consumer-e2e/clients.playwright.ts";
const regression = "apps/web/src/components/star-ring/StarRing.contract.test.tsx";
const onboarding = "apps/web/src/pages/onboarding/Onboarding.tsx";
const banner = "apps/web/src/components/SimBanner.tsx";
const modeRegression = "apps/web/src/pages/onboarding/Onboarding.mode.test.tsx";
const ball = "apps/web/src/components/loomball/LoomBall.tsx";
const ballRegression = "apps/web/src/components/loomball/LoomBall.accessibility.test.tsx";
const levelBadge = "apps/web/src/components/hud/gamification.tsx";
const profilePage = "apps/web/src/pages/p8/P8.tsx";
const levelRegression = "scripts/acceptance/lib/level-badge-layout.test.mjs";
const loomMate = "apps/web/src/components/loommate/LoomMate.tsx";
const unreadRegression = "scripts/acceptance/lib/loommate-unread-contrast.test.mjs";
const viewportRegression = "scripts/acceptance/lib/loommate-viewport.test.mjs";
const skillBanner = "apps/web/src/components/SkillDistBanner.tsx";
const stackingRegression = "scripts/acceptance/lib/loommate-stacking.test.mjs";
const skillsPage = "apps/web/src/pages/p6/P6.tsx";
const suggestionRegression = "scripts/acceptance/lib/skill-suggestion-layout.test.mjs";
const notificationRegression = "scripts/acceptance/lib/task-notification-interception.test.mjs";
const settingsRaceRegression = "apps/web/src/components/loommate/LoomMate.settings-race.test.tsx";
const taskPage = "apps/web/src/pages/p2/P2.tsx";
const taskPlanRegression = "scripts/acceptance/lib/task-plan-visibility.test.mjs";
const manifestFile = ".workloom-client-repairs.json";
const currentManifestFile = ".workloom-client-repairs-T-2026-1027-0001.json";
const webIndex = "apps/web/index.html";
const shellBridge = "apps/web/src/shell/Bridge.tsx";
const fontRegression = "scripts/acceptance/lib/session.test.mjs";
const bridgeRegression = "scripts/acceptance/lib/bridge-emergency-brake.test.mjs";
const write = (root, path, content) => { mkdirSync(dirname(join(root, path)), { recursive: true }); writeFileSync(join(root, path), content); };

function fixture(t, repaired = true) {
  const root = mkdtempSync(join(tmpdir(), "tiger-client-repairs-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const g = join(root, "governance");
  const entries = ["apps/web/src/main.tsx", "apps/webb/src/main.tsx", "apps/webc/src/main.tsx", "apps/webc/public/service-front.config.json", star, api, consumer, onboarding, banner, ball, levelBadge, profilePage, loomMate, skillBanner, skillsPage, taskPage, webIndex, shellBridge];
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
    for (const [path, card] of [[star, "MC-012"], [api, "MC-018"], [consumer, "MC-015"], [onboarding, "MC-027"], [banner, "MC-027"], [ball, "MC-032"], [levelBadge, "MC-033"], [profilePage, "MC-033"], [loomMate, "MC-034"], [skillBanner, "MC-037"], [skillsPage, "MC-038"], [taskPage, "MC-041"]]) {
      write(g, path, `reviewed repair:${path}\n`);
      manifest.repairs.push({ path, card, upstreamSha256: state.managedFiles[path].sha256, repairedSha256: digest(readFileSync(join(g, path))), sourceCommit: "a".repeat(40), reason: "Reviewed contract repair with focused behavioral regression coverage." });
    }
    write(g, regression, "test('real component contract', () => {});\n");
    manifest.testAdditions.push({ path: regression, card: "MC-012", sha256: digest(readFileSync(join(g, regression))) });
    write(g, modeRegression, "test('mode declaration and formal qualification', () => {});\n");
    manifest.testAdditions.push({ path: modeRegression, card: "MC-027", sha256: digest(readFileSync(join(g, modeRegression))) });
    write(g, ballRegression, "test('decorative animation stays outside sequential keyboard focus', () => {});\n");
    manifest.testAdditions.push({ path: ballRegression, card: "MC-032", sha256: digest(readFileSync(join(g, ballRegression))) });
    for (const [path, card] of [[levelRegression, "MC-033"], [unreadRegression, "MC-034"], [viewportRegression, "MC-035"], [stackingRegression, "MC-037"], [suggestionRegression, "MC-038"], [notificationRegression, "MC-039"], [taskPlanRegression, "MC-041"], [settingsRaceRegression, "MC-054"]]) {
      write(g, path, "test('real browser component evidence', () => {});\n");
      manifest.testAdditions.push({ path, card, sha256: digest(readFileSync(join(g, path))) });
    }
    write(g, manifestFile, JSON.stringify(manifest));
  }
  return { root, g, manifest, state, save: () => write(g, manifestFile, JSON.stringify(manifest)) };
}

function addCurrentManifest(f) {
  const manifest = {
    schemaVersion: "workloom.tiger-client-repairs/v1",
    repository: "geniusdapeng-collab/workroom-tiger",
    taskId: "T-2026-1027-0001",
    authorization: {
      basis: "user-requested-repository-repair",
      issue: "https://github.com/geniusdapeng-collab/workroom-tiger/issues/39",
      pullRequest: "https://github.com/geniusdapeng-collab/workroom-tiger/pull/40",
    },
    base: {
      version: f.state.version,
      sourceCommit: f.state.sourceCommit,
      stateSha256: digest(readFileSync(join(f.g, ".workloom-client-foundation.json"))),
      verifierSha256: digest(readFileSync(join(f.g, "scripts/verify-client-foundation-consumer.mjs"))),
    },
    repairs: [],
    testAdditions: [],
  };
  for (const [path, card, sourceCommit] of [
    [webIndex, "MC-055", "e".repeat(40)],
    [shellBridge, "MC-056", "f".repeat(40)],
  ]) {
    write(f.g, path, `reviewed current repair:${path}\n`);
    manifest.repairs.push({
      path,
      card,
      upstreamSha256: f.state.managedFiles[path].sha256,
      repairedSha256: digest(readFileSync(join(f.g, path))),
      sourceCommit,
      reason: "Reviewed startup or emergency-control repair with exact focused regression coverage.",
    });
  }
  for (const [path, card] of [[fontRegression, "MC-055"], [bridgeRegression, "MC-056"]]) {
    write(f.g, path, `test('current repair ${card}', () => {});\n`);
    manifest.testAdditions.push({ path, card, sha256: digest(readFileSync(join(f.g, path))) });
  }
  write(f.g, currentManifestFile, JSON.stringify(manifest));
  return manifest;
}

test("an unchanged upstream consumer retains the strict gate without a local manifest", (t) => {
  assert.deepEqual(verifyTigerClientRepairs(fixture(t, false).root), []);
});
test("exact reviewed repairs and the bound regression file pass without rewriting upstream state", (t) => {
  const f = fixture(t); const before = readFileSync(join(f.g, ".workloom-client-foundation.json"));
  assert.deepEqual(verifyTigerClientRepairs(f.root), []);
  assert.deepEqual(readFileSync(join(f.g, ".workloom-client-foundation.json")), before);
});
test("a second exact reviewed task manifest removes only its registered drift", (t) => {
  const f = fixture(t);
  addCurrentManifest(f);
  assert.deepEqual(verifyTigerClientRepairs(f.root), []);
});
test("current task authorization and emergency-brake regression remain exact", (t) => {
  const f = fixture(t);
  const current = addCurrentManifest(f);
  current.authorization.issue = "https://github.com/geniusdapeng-collab/workroom-tiger/issues/24";
  write(f.g, currentManifestFile, JSON.stringify(current));
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
  current.authorization.issue = "https://github.com/geniusdapeng-collab/workroom-tiger/issues/39";
  write(f.g, currentManifestFile, JSON.stringify(current));
  write(f.g, bridgeRegression, "changed after review\n");
  assert.ok(verifyTigerClientRepairs(f.root).length > 0);
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
test("the product-specific consumer assertion remains bound to its reviewed bytes", (t) => {
  const f = fixture(t); write(f.g, consumer, "unreviewed weakened assertion\n");
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
for (const path of [onboarding, banner]) {
  test(`MC027 reviewed source ${path} rejects later byte changes`, (t) => {
    const f = fixture(t); write(f.g, path, "unreviewed mode claim\n");
    assert.ok(verifyTigerClientRepairs(f.root).some((e) => e.includes(path)));
  });
  test(`MC027 source ${path} requires the exact card`, (t) => {
    const f = fixture(t);
    f.manifest.repairs.find((entry) => entry.path === path).card = "MC-012";
    f.save();
    assert.ok(verifyTigerClientRepairs(f.root).length > 0);
  });
}
for (const kind of ["missing-registration", "changed-bytes", "wrong-card", "duplicate", "executable", "symlink"]) {
  test(`MC027 mode regression ${kind} fails closed`, (t) => {
    const f = fixture(t);
    const registered = f.manifest.testAdditions.find((entry) => entry.path === modeRegression);
    if (kind === "missing-registration") f.manifest.testAdditions = f.manifest.testAdditions.filter((entry) => entry.path !== modeRegression);
    if (kind === "changed-bytes") write(f.g, modeRegression, "unreviewed tests\n");
    if (kind === "wrong-card") registered.card = "MC-012";
    if (kind === "duplicate") f.manifest.testAdditions[1] = { ...f.manifest.testAdditions[0] };
    if (kind === "executable") chmodSync(join(f.g, modeRegression), 0o755);
    if (kind === "symlink") {
      copyFileSync(join(f.g, modeRegression), join(f.root, "outside-mode.test.tsx"));
      rmSync(join(f.g, modeRegression));
      symlinkSync(join(f.root, "outside-mode.test.tsx"), join(f.g, modeRegression));
    }
    f.save();
    assert.ok(verifyTigerClientRepairs(f.root).length > 0);
  });
}
test("MC027 regression cannot authorize an unrelated new client source", (t) => {
  const f = fixture(t);
  write(f.g, "apps/web/src/pages/onboarding/unreviewed.ts", "export const invented = 'simulated';\n");
  assert.ok(verifyTigerClientRepairs(f.root).some((e) => e.includes("unreviewed.ts")));
});
for (const kind of ["changed-source", "missing-source-registration", "wrong-source-card", "missing-test-registration", "changed-test", "wrong-test-card", "executable-test", "symlink-test"]) {
  test(`MC032 decorative focus repair ${kind} fails closed`, (t) => {
    const f = fixture(t);
    const repair = f.manifest.repairs.find((entry) => entry.path === ball);
    const regression = f.manifest.testAdditions.find((entry) => entry.path === ballRegression);
    if (kind === "changed-source") write(f.g, ball, "unreviewed focus behavior\n");
    if (kind === "missing-source-registration") f.manifest.repairs = f.manifest.repairs.filter((entry) => entry.path !== ball);
    if (kind === "wrong-source-card") repair.card = "MC-012";
    if (kind === "missing-test-registration") f.manifest.testAdditions = f.manifest.testAdditions.filter((entry) => entry.path !== ballRegression);
    if (kind === "changed-test") write(f.g, ballRegression, "unreviewed accessibility assertions\n");
    if (kind === "wrong-test-card") regression.card = "MC-012";
    if (kind === "executable-test") chmodSync(join(f.g, ballRegression), 0o755);
    if (kind === "symlink-test") {
      copyFileSync(join(f.g, ballRegression), join(f.root, "outside-ball.test.tsx"));
      rmSync(join(f.g, ballRegression));
      symlinkSync(join(f.root, "outside-ball.test.tsx"), join(f.g, ballRegression));
    }
    f.save();
    assert.ok(verifyTigerClientRepairs(f.root).length > 0);
  });
}
for (const [card, source, regressionPath] of [
  ["MC-033", levelBadge, levelRegression],
  ["MC-033", profilePage, levelRegression],
  ["MC-034", loomMate, unreadRegression],
  ["MC-035", loomMate, viewportRegression],
  ["MC-037", banner, stackingRegression],
  ["MC-037", skillBanner, stackingRegression],
  ["MC-038", skillsPage, suggestionRegression],
  ["MC-039", loomMate, notificationRegression],
  ["MC-041", taskPage, taskPlanRegression],
  ["MC-054", loomMate, settingsRaceRegression],
]) {
  for (const kind of ["changed-source", "missing-source-registration", "wrong-source-card", "missing-test-registration", "changed-test", "wrong-test-card", "executable-test", "symlink-test"]) {
    test(`${card} ${source} ${card === "MC-054" ? "component" : "browser"} regression binding ${kind} fails closed`, (t) => {
      const f = fixture(t);
      const repair = f.manifest.repairs.find((entry) => entry.path === source);
      const boundTest = f.manifest.testAdditions.find((entry) => entry.path === regressionPath);
      if (kind === "changed-source") write(f.g, source, "unreviewed component change\n");
      if (kind === "missing-source-registration") f.manifest.repairs = f.manifest.repairs.filter((entry) => entry.path !== source);
      if (kind === "wrong-source-card") repair.card = "MC-012";
      if (kind === "missing-test-registration") f.manifest.testAdditions = f.manifest.testAdditions.filter((entry) => entry.path !== regressionPath);
      if (kind === "changed-test") write(f.g, regressionPath, "unreviewed browser assertions\n");
      if (kind === "wrong-test-card") boundTest.card = "MC-012";
      if (kind === "executable-test") chmodSync(join(f.g, regressionPath), 0o755);
      if (kind === "symlink-test") {
        const external = join(f.root, `outside-${card}.test.mjs`);
        copyFileSync(join(f.g, regressionPath), external);
        rmSync(join(f.g, regressionPath));
        symlinkSync(external, join(f.g, regressionPath));
      }
      f.save();
      assert.ok(verifyTigerClientRepairs(f.root).length > 0);
    });
  }
}
