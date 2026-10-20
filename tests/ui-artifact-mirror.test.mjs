import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const governance = join(dirname(dirname(fileURLToPath(import.meta.url))), "governance");
const state = JSON.parse(readFileSync(join(governance, ".workloom-ui.json"), "utf8"));
const artifact = state.artifact;
const packageKey = `${state.package}@${artifact.url}`;
const resolution = () => ({ tarball: artifact.url, integrity: artifact.sha512 });

function fixture(t) {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "tiger-ui-artifact-mirror-")));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const target = join(root, "vendor", "ui-artifacts", artifact.assetName);
  mkdirSync(dirname(target), { recursive: true });
  for (const file of [".pnpmfile.cjs", ".workloom-ui.json"]) {
    copyFileSync(join(governance, file), join(root, file));
  }
  copyFileSync(join(governance, "vendor", "ui-artifacts", artifact.assetName), target);
  return { root, target, hooks: require(join(root, ".pnpmfile.cjs")) };
}

function wantedLockfile() {
  return { packages: {
    [`${packageKey}(react-dom@19.2.8(react@19.2.8))(react@19.2.8)`]: {
      version: state.version,
      resolution: resolution(),
    },
  } };
}

function fetchers() {
  const calls = [];
  return { calls, defaults: {
    localTarball: (...args) => { calls.push({ kind: "local", args }); return "from-mirror"; },
    remoteTarball: (...args) => { calls.push({ kind: "remote", args }); return "from-remote"; },
  } };
}

test("vendored UI archive has exactly the existing approved SHA-512", () => {
  const bytes = readFileSync(join(governance, "vendor", "ui-artifacts", artifact.assetName));
  assert.equal(`sha512-${createHash("sha512").update(bytes).digest("base64")}`, artifact.sha512);
});

test("pnpmfile checksum binds the complete transport hook in the frozen lockfile", () => {
  const source = readFileSync(join(governance, ".pnpmfile.cjs"), "utf8").replaceAll("\r\n", "\n");
  const checksum = `sha256-${createHash("sha256").update(source).digest("base64")}`;
  const lock = readFileSync(join(governance, "pnpm-lock.yaml"), "utf8");
  assert.equal(lock.match(/^pnpmfileChecksum: (.+)$/m)?.[1], checksum);
  assert.ok(!lock.includes("tarball: file:"), "local transport paths must never enter the lockfile");
});

test("pnpm 10 delegates only the pinned archive without mutating its resolution", (t) => {
  const f = fixture(t);
  const { calls, defaults } = fetchers();
  const fetch = f.hooks.hooks.fetchers.remoteTarball({ defaultFetchers: defaults });
  const original = Object.freeze(resolution());
  const cafs = {};
  const options = { lockfileDir: f.root };
  assert.equal(fetch(cafs, original, options), "from-mirror");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].kind, "local");
  assert.strictEqual(calls[0].args[0], cafs);
  assert.strictEqual(calls[0].args[2], options);
  assert.deepEqual(calls[0].args[1], { ...original, tarball: `file:${f.target}` });
  assert.deepEqual(original, resolution());
  const other = Object.freeze({ tarball: "https://registry.npmjs.org/example.tgz", integrity: "other" });
  assert.equal(fetch(cafs, other, options), "from-remote");
  assert.strictEqual(calls[1].args[1], other);
});

test("pnpm 11 fetcher preserves the same approved bytes and unrelated routing", (t) => {
  const f = fixture(t);
  const { calls, defaults } = fetchers();
  const [fetcher] = f.hooks.fetchers;
  const original = Object.freeze(resolution());
  assert.equal(fetcher.canFetch(packageKey, original), true);
  assert.equal(fetcher.canFetch("other", { tarball: "https://example.com/other.tgz" }), false);
  assert.equal(fetcher.fetch({}, original, {}, defaults), "from-mirror");
  assert.deepEqual(calls[0].args[1], { ...original, tarball: `file:${f.target}` });
  assert.deepEqual(original, resolution());
});

test("fresh resolution receives the approved integrity before local extraction", (t) => {
  const f = fixture(t);
  const { calls, defaults } = fetchers();
  f.hooks.hooks.fetchers.remoteTarball({ defaultFetchers: defaults })({}, { tarball: artifact.url }, {});
  assert.equal(calls[0].args[1].integrity, artifact.sha512);
});

test("pre-resolution validation leaves wanted and current lockfiles unchanged", (t) => {
  const f = fixture(t);
  const options = { wantedLockfile: wantedLockfile(), currentLockfile: wantedLockfile() };
  const before = structuredClone(options);
  f.hooks.hooks.preResolution(options);
  assert.deepEqual(options, before);
});

test("configuration validation catches mirror corruption before optimistic repeat installs", (t) => {
  const f = fixture(t);
  const config = Object.freeze({ frozenLockfile: true });
  assert.strictEqual(f.hooks.hooks.updateConfig(config), config);
  writeFileSync(f.target, "unapproved replacement");
  assert.throws(() => f.hooks.hooks.updateConfig(config), /UI artifact mirror.*integrity/i);
});

test("missing and corrupted mirrors fail even when pnpm could reuse cached files", async (t) => {
  for (const corruption of ["missing", "corrupt"]) {
    await t.test(corruption, (t) => {
      const f = fixture(t);
      if (corruption === "missing") rmSync(f.target);
      else writeFileSync(f.target, "unapproved replacement");
      assert.throws(() => f.hooks.hooks.preResolution({ wantedLockfile: wantedLockfile() }),
        /UI artifact mirror.*(read|integrity)/i);
    });
  }
});

test("changed consumer identity, version, URL, checksum, or asset cannot reuse the mirror", async (t) => {
  const changes = [
    ["package", (s) => { s.package = "@other/ui"; }],
    ["version", (s) => { s.version = "0.1.18"; }],
    ["URL", (s) => { s.artifact.url += "?unapproved=1"; }],
    ["checksum", (s) => { s.artifact.sha512 = "sha512-changed"; }],
    ["asset", (s) => { s.artifact.assetName = "other.tgz"; }],
  ];
  for (const [name, change] of changes) {
    await t.test(name, (t) => {
      const f = fixture(t);
      const changed = structuredClone(state);
      change(changed);
      writeFileSync(join(f.root, ".workloom-ui.json"), JSON.stringify(changed));
      const { defaults } = fetchers();
      const fetch = f.hooks.hooks.fetchers.remoteTarball({ defaultFetchers: defaults });
      assert.throws(() => fetch({}, resolution(), {}),
        /UI artifact mirror.*consumer state/i);
    });
  }
});

test("lockfile integrity, tarball, and version drift fail before fetching", async (t) => {
  const changes = [
    ["integrity", (p) => { p.resolution.integrity = "sha512-changed"; }],
    ["missing integrity", (p) => { delete p.resolution.integrity; }],
    ["tarball", (p) => { p.resolution.tarball = "file:/unapproved.tgz"; }],
    ["version", (p) => { p.version = "0.1.18"; }],
  ];
  for (const [name, change] of changes) {
    await t.test(name, (t) => {
      const f = fixture(t);
      const lock = wantedLockfile();
      change(Object.values(lock.packages)[0]);
      const before = structuredClone(lock);
      assert.throws(() => f.hooks.hooks.preResolution({ wantedLockfile: lock }),
        /UI artifact mirror.*lockfile/i);
      assert.deepEqual(lock, before);
    });
  }
});

test("a new approved UI release keeps its ordinary remote transport", (t) => {
  const f = fixture(t);
  const upgraded = structuredClone(state);
  upgraded.version = "0.1.18";
  upgraded.artifact.url = artifact.url.replaceAll("0.1.17", "0.1.18");
  upgraded.artifact.assetName = "workloom-ui-0.1.18.tgz";
  writeFileSync(join(f.root, ".workloom-ui.json"), JSON.stringify(upgraded));
  rmSync(f.target);
  // During a normal upgrade, manifests and consumer state change before pnpm
  // replaces the old wanted lockfile. The old release must not block resolution.
  const upgrading = { wantedLockfile: wantedLockfile(), currentLockfile: wantedLockfile() };
  const before = structuredClone(upgrading);
  f.hooks.hooks.preResolution(upgrading);
  assert.deepEqual(upgrading, before);
  const { calls, defaults } = fetchers();
  const next = { tarball: upgraded.artifact.url, integrity: "new-release-integrity" };
  assert.equal(f.hooks.hooks.fetchers.remoteTarball({ defaultFetchers: defaults })({}, next, {}), "from-remote");
  assert.strictEqual(calls[0].args[1], next);
});
