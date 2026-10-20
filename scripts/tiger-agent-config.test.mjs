import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, link, mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import fs, { existsSync } from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { homedir, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { parseOptions, prepareConfiguration, writeConfiguration } from "./tiger-agent-config.mjs";

const root = resolve(import.meta.dirname, "..");
const python = process.env.TIGER_TEST_PYTHON ?? join(root, ".venv/bin/python");
const BEGIN = "# BEGIN TIGER AGENT MCP v1";
const END = "# END TIGER AGENT MCP v1";
async function fixture(t) {
  const temporary = await mkdtemp(join(tmpdir(), "tiger config 中文 "));
  const project = await realpath(temporary);
  t.after(() => rm(project, { recursive: true, force: true }));
  const file = async (name, text) => { const path = join(project, name); await mkdir(join(path, ".."), { recursive: true }); await writeFile(path, text); return path; };
  await file("product.manifest.json", JSON.stringify({ productId: "workroom-tiger" }));
  await file("scripts/tiger-agent-mcp.mjs", "// Configuration target fixture only.\n");
  return { project, file, options: { project, python, client: "codex" } };
}
function withoutBlock(text) { return text.replace(/^# BEGIN TIGER AGENT MCP v1[\s\S]*?^# END TIGER AGENT MCP v1/gmu, "<owned>"); }

test("R04: Codex preserves unrelated bytes, supports spaces/Unicode, is idempotent and changes only owned access mode", async (t) => {
  const f = await fixture(t);
  const original = '# User comment 中文\nmodel = "unchanged-model"\n\n[mcp_servers.existing]\ncommand = "existing command"\nargs = ["a b", "中文"]\n';
  await f.file(".codex/config.toml", original);
  const prepared = prepareConfiguration(f.options);
  assert.equal(prepared.mode, "readonly"); assert.ok(prepared.proposed.startsWith(original));
  assert.equal(writeConfiguration(prepared).durability, "synced");
  const again = prepareConfiguration(f.options);
  assert.equal(again.changed, false); assert.deepEqual(writeConfiguration(again), { changed: false, matches: true });
  const paper = prepareConfiguration({ ...f.options, mode: "paper" });
  assert.equal(withoutBlock(paper.proposed), withoutBlock(again.proposed));
  assert.match(paper.proposed, /"--mode",\s*"paper"/u); writeConfiguration(paper);
  const readonly = prepareConfiguration(f.options); writeConfiguration(readonly);
  assert.equal(await readFile(readonly.file, "utf8"), again.proposed);
});

test("R04: DSH block and flow lists preserve plugins and owned separators across repeated generation", async (t) => {
  const originals = ["", "[]\n", '# original\n- id: other\n  name: other-plugin\n  config: {value: "中文"}\n',
    '[{id: other, name: other-plugin}]\n', '[{id: other, name: other-plugin}, # existing separator\n]\n'];
  for (const original of originals) {
    const f = await fixture(t); const options = { ...f.options, client: "dsh" };
    await f.file(".dsh/tiger-mcp.yaml", original);
    const prepared = prepareConfiguration(options); writeConfiguration(prepared);
    const again = prepareConfiguration(options); assert.equal(again.changed, false, original); writeConfiguration(again);
    const paper = prepareConfiguration({ ...options, mode: "paper" });
    assert.equal(withoutBlock(paper.proposed), withoutBlock(prepared.proposed)); writeConfiguration(paper);
    assert.equal(prepareConfiguration({ ...options, mode: "paper" }).changed, false);
  }
});

test("P04/R04: DSH !!js text stays inert and byte-identical while unrelated alias data is preserved", async (t) => {
  const f = await fixture(t);
  const canary = join(f.project, "must-not-exist");
  const original = `# Dynamic DSH plugin values are never evaluated by this generator.\n- id: other\n  name: example\n  config:\n    payload: !!js >-\n      require('node:fs').writeFileSync(${JSON.stringify(canary)}, 'executed')\n    first: &data {message: unchanged}\n    second: *data\n`;
  await f.file(".dsh/tiger-mcp.yaml", original);
  const prepared = prepareConfiguration({ ...f.options, client: "dsh" });
  assert.ok(prepared.proposed.startsWith(original)); writeConfiguration(prepared);
  assert.equal(existsSync(canary), false);
  assert.equal(prepareConfiguration({ ...f.options, client: "dsh" }).changed, false);
});

test("R04: malformed inputs, conflicting keys, unrelated managed content and duplicate markers leave bytes unchanged", async (t) => {
  const cases = [
    ["codex", '[invalid\n'], ["codex", '[mcp_servers.tiger-workflow]\ncommand="other"\n'],
    ["codex", `${BEGIN}\n[mcp_servers.tiger-workflow]\ncommand="old"\n[unrelated]\nkeep=true\n${END}\n`],
    ["codex", `${BEGIN}\n${BEGIN}\n${END}\n`], ["codex", `${END}\n${BEGIN}\n`],
    ["dsh", 'plugins: []\n'], ["dsh", '- id: duplicate\n  name: a\n- id: duplicate\n  name: b\n'],
    ["dsh", '- id: mcp-tiger-workflow\n  name: another\n'],
    ["dsh", '- id: existing\n  name: "@deepseek-ai/dsh-mcp-client"\n  config: {serverName: tiger-workflow}\n'],
    ["dsh", `${BEGIN}\n- id: mcp-tiger-workflow\n  name: old\n- id: unrelated\n  name: other\n${END}\n`],
  ];
  for (const [client, original] of cases) {
    const f = await fixture(t); const name = client === "codex" ? ".codex/config.toml" : ".dsh/tiger-mcp.yaml";
    const target = await f.file(name, original);
    assert.throws(() => prepareConfiguration({ ...f.options, client }), { code: "INVALID_REQUEST" });
    assert.equal(await readFile(target, "utf8"), original);
  }
});

test("P04/B05/R04: traversal, symlink and hardlink configurations and user-global project roots are rejected", async (t) => {
  const f = await fixture(t);
  for (const config of ["../outside.toml", "/tmp/outside.toml", "x/../outside.toml", "x//a.toml", "x\\..\\outside.toml"]) {
    assert.throws(() => prepareConfiguration({ ...f.options, config }), { code: "INVALID_REQUEST" });
  }
  assert.throws(() => prepareConfiguration({ ...f.options, project: homedir() }), { code: "INVALID_REQUEST" });
  const outside = await mkdtemp(join(tmpdir(), "tiger-config-canary-")); t.after(() => rm(outside, { recursive: true, force: true }));
  const canary = join(outside, "canary.toml"); await writeFile(canary, '# must remain unchanged\n');
  for (const kind of ["symlink", "hardlink", "parent"]) {
    const name = kind === "parent" ? "linked/canary.toml" : `${kind}.toml`;
    if (kind === "parent") await symlink(outside, join(f.project, "linked"));
    else await (kind === "symlink" ? symlink(canary, join(f.project, name)) : link(canary, join(f.project, name)));
    assert.throws(() => prepareConfiguration({ ...f.options, config: name }), { code: "INVALID_REQUEST" });
  }
  assert.equal(await readFile(canary, "utf8"), '# must remain unchanged\n');
});

test("R04: --check creates no directory, concurrent changes and existing write lock prevent replacement", async (t) => {
  const f = await fixture(t);
  const check = prepareConfiguration({ ...f.options, check: true });
  assert.deepEqual(writeConfiguration(check), { changed: false, matches: false });
  assert.equal(existsSync(join(f.project, ".codex")), false);
  const prepared = prepareConfiguration(f.options); await f.file(".codex/config.toml", '# changed after preparation\n');
  assert.throws(() => writeConfiguration(prepared), { code: "IDEMPOTENCY_CONFLICT" });
  const ready = prepareConfiguration(f.options); await f.file(".codex/.tiger-agent-config.lock", "existing");
  assert.throws(() => writeConfiguration(ready), { code: "IDEMPOTENCY_CONFLICT" });
  assert.equal(await readFile(ready.file, "utf8"), '# changed after preparation\n');
  await rm(join(f.project, ".codex/.tiger-agent-config.lock"));
  writeConfiguration(ready);
  assert.equal(existsSync(join(f.project, ".codex/.tiger-agent-config.lock")), false);
});

test("B01/R04: invalid startup/Python selection fails before writing and older validator responses are rejected", async (t) => {
  const f = await fixture(t);
  for (const extra of [{ mode: "live" }, { python: "/no/such/python" }, { python: "--unsafe" }, { stateRoot: "relative" }, { unknown: true }]) {
    assert.throws(() => prepareConfiguration({ ...f.options, ...extra }), { code: "INVALID_REQUEST" });
  }
  const old = await f.file("old-python", `#!${process.execPath}\nprocess.stdout.write(JSON.stringify({python:'/trusted/python',version:[3,10,99],value:{}}));\n`);
  await chmod(old, 0o700);
  assert.throws(() => prepareConfiguration({ ...f.options, python: old }), { code: "INVALID_REQUEST" });
  assert.equal(existsSync(join(f.project, ".codex")), false);
  for (const args of [["--client", "codex", "--client", "dsh"], ["--python"], ["--global"], ["--check", "extra"]]) assert.throws(() => parseOptions(args));
});

test("R04: generator CLI reports real validation, explicit paper and host attachment honestly", async (t) => {
  const f = await fixture(t);
  const script = join(root, "scripts/tiger-agent-config.mjs");
  const args = [script, "--client", "codex", "--project", f.project, "--python", python];
  const checked = spawnSync(process.execPath, [...args, "--check"], { encoding: "utf8" });
  assert.equal(checked.status, 2); assert.equal(JSON.parse(checked.stdout).host_attached, false);
  assert.equal(existsSync(join(f.project, ".codex")), false);
  const generated = spawnSync(process.execPath, [...args, "--mode", "paper"], { encoding: "utf8" });
  assert.equal(generated.status, 0, generated.stdout); assert.equal(generated.stderr, "");
  const result = JSON.parse(generated.stdout); assert.equal(result.ok, true); assert.equal(result.host_attached, false);
  assert.equal(result.mode, "paper"); assert.ok(result.python_version[1] >= 11);
  const matching = spawnSync(process.execPath, [...args, "--mode", "paper", "--check"], { encoding: "utf8" });
  assert.equal(matching.status, 0); assert.equal(JSON.parse(matching.stdout).changed, false);
});

test("R04: write faults preserve prior bytes before rename and disclose visible commits after rename", async (t) => {
  // Official builtin live-binding API keeps fault injection confined to this test's synchronous commit.
  // https://nodejs.org/api/module.html#modulesyncbuiltinesmexports
  for (const fault of ["file-sync", "rename", "directory-sync", "lock-cleanup"]) {
    const f = await fixture(t); const original = '# prior configuration\n';
    await f.file(".codex/config.toml", original);
    const prepared = prepareConfiguration(f.options);
    const saved = { fsyncSync: fs.fsyncSync, renameSync: fs.renameSync, unlinkSync: fs.unlinkSync };
    let syncs = 0;
    const failed = () => { const error = new Error("Injected configuration I/O fault"); error.code = "EIO"; throw error; };
    fs.fsyncSync = (...args) => { syncs += 1; if ((fault === "file-sync" && syncs === 1) || (fault === "directory-sync" && syncs === 2)) failed(); return saved.fsyncSync(...args); };
    fs.renameSync = (...args) => { if (fault === "rename") failed(); return saved.renameSync(...args); };
    fs.unlinkSync = (...args) => { if (fault === "lock-cleanup" && String(args[0]).endsWith(".tiger-agent-config.lock")) failed(); return saved.unlinkSync(...args); };
    syncBuiltinESMExports();
    let outcome;
    try {
      if (["file-sync", "rename"].includes(fault)) assert.throws(() => writeConfiguration(prepared), { code: "EIO" });
      else outcome = writeConfiguration(prepared);
    } finally { Object.assign(fs, saved); syncBuiltinESMExports(); }
    if (["file-sync", "rename"].includes(fault)) assert.equal(await readFile(prepared.file, "utf8"), original);
    else {
      assert.equal(await readFile(prepared.file, "utf8"), prepared.proposed); assert.equal(outcome.changed, true); assert.equal(outcome.matches, true);
      if (fault === "directory-sync") assert.equal(outcome.durability, "unknown");
      else assert.equal(outcome.cleanup, "incomplete");
      assert.match(outcome.warning, /visible|replaced/u);
    }
    assert.equal(fs.readdirSync(join(f.project, ".codex")).some((name) => name.endsWith(".tmp")), false);
    assert.equal(existsSync(join(f.project, ".codex/.tiger-agent-config.lock")), fault === "lock-cleanup");
  }
});

test("NP002/R04: changes during temporary-file sync are rechecked before publication", async (t) => {
  for (const change of ["edit", "replace-same-bytes", "create", "delete", "permissions"]) await t.test(change, async (scenario) => {
    const f = await fixture(scenario); const original = '# original user configuration\n';
    const target = join(f.project, ".codex/config.toml");
    if (change !== "create") await f.file(".codex/config.toml", original);
    const prepared = prepareConfiguration(f.options);
    const saved = fs.fsyncSync;
    let syncs = 0;
    let external;
    fs.fsyncSync = (...args) => {
      syncs += 1;
      if (syncs === 1) {
        if (change === "edit" || change === "create") fs.writeFileSync(target, '# concurrent user save\n');
        else if (change === "replace-same-bytes") {
          fs.writeFileSync(`${target}.user-save`, original); fs.renameSync(`${target}.user-save`, target);
        } else if (change === "delete") fs.unlinkSync(target);
        else fs.chmodSync(target, 0o400);
        external = existsSync(target) ? { text: fs.readFileSync(target, "utf8"), ino: fs.statSync(target).ino,
          mode: fs.statSync(target).mode & 0o777 } : null;
      }
      return saved(...args);
    };
    syncBuiltinESMExports();
    try { assert.throws(() => writeConfiguration(prepared), { code: "IDEMPOTENCY_CONFLICT" }); }
    finally { fs.fsyncSync = saved; syncBuiltinESMExports(); }
    const after = existsSync(target) ? { text: fs.readFileSync(target, "utf8"), ino: fs.statSync(target).ino,
      mode: fs.statSync(target).mode & 0o777 } : null;
    assert.deepEqual(after, external);
    assert.equal(fs.readdirSync(join(f.project, ".codex")).some((name) => name.endsWith(".tmp") || name.endsWith(".lock")), false);
  });
});
