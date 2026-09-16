import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const governanceRoot = resolve(import.meta.dirname, "..");
const repositoryRoot = resolve(governanceRoot, "..");
const product = JSON.parse(readFileSync(resolve(repositoryRoot, "product.manifest.json"), "utf8"));
const seed = readFileSync(resolve(governanceRoot, "scripts/seed-trading.ts"), "utf8");

test("桌面交易种子满足游客进场的工作区、成员与唯一 Bundle 身份", () => {
  assert.equal(product.defaultBundle, "trading");
  assert.equal(product.demoWorkspaceSlug, "tiger-trading");
  assert.equal(product.demoMemberNo, "MEM-T001");
  assert.match(seed, /bundle_id, is_example/u);
  assert.match(seed, /MEM-T001/u);
  assert.match(seed, /INSERT INTO bundle_installs/u);
  assert.match(seed, /bundle_id<>'trading' AND status='active'/u);
  assert.match(seed, /'trading',[\s\S]*'active'/u);
  assert.match(seed, /ON CONFLICT \(id\) DO UPDATE SET bundle_id='trading', status='active'/u);
});
