import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const governanceRoot = resolve(import.meta.dirname, "..");
const repositoryRoot = resolve(governanceRoot, "..");
const workflow = readFileSync(resolve(repositoryRoot, ".github/workflows/build-desktop.yml"), "utf8");
const builder = readFileSync(resolve(governanceRoot, "electron-builder.yml"), "utf8");
const siteZh = readFileSync(resolve(governanceRoot, "apps/site/index.html"), "utf8");
const siteEn = readFileSync(resolve(governanceRoot, "apps/site/en.html"), "utf8");
const verifier = readFileSync(resolve(governanceRoot, "scripts/verify-product-content.mjs"), "utf8");
const packageJson = JSON.parse(readFileSync(resolve(governanceRoot, "package.json"), "utf8"));
const product = JSON.parse(readFileSync(resolve(repositoryRoot, "product.manifest.json"), "utf8"));

test("仅允许从 main 手动发布，默认 unsigned 并保留 signed/unsigned 两条路径", () => {
  assert.match(workflow, /platform_signing:[\s\S]*options: \[signed, unsigned\][\s\S]*default: unsigned/u);
  assert.match(workflow, /on:\s*\n\s*workflow_dispatch:/u);
  assert.doesNotMatch(workflow, /push:\s*\n\s*tags:/u);
  assert.match(workflow, /PLATFORM_SIGNING: \$\{\{ inputs\.platform_signing \}\}/u);
  assert.match(workflow, /test "\$GITHUB_REF" = "refs\/heads\/main"/u);
  assert.match(workflow, /if: env\.PLATFORM_SIGNING == 'signed'/u);
  assert.match(workflow, /if: env\.PLATFORM_SIGNING == 'unsigned'/u);
});

test("平台 unsigned 不放松内部 Bundle 签名与信任环", () => {
  assert.ok((workflow.match(/BUNDLE_SIGNING_PRIVATE_KEY/g) ?? []).length >= 4);
  assert.ok((workflow.match(/pnpm bundle:release/g) ?? []).length >= 2);
  assert.ok((workflow.match(/test -s build\/bundle-trust\.json/g) ?? []).length >= 2);
  assert.ok((workflow.match(/test -s "\$RES\/bundle-trust\.json"/g) ?? []).length >= 3);
  assert.match(builder, /from: build\/bundle-trust\.json[\s\S]*to: bundle-trust\.json/u);
  assert.equal(packageJson.scripts["bundle:release"], "tsx scripts/bundle-governance.mts --refresh-digests --release");
});

test("清洁依赖安装后先构建行业契约再构建 Web", () => {
  const buildJobs = workflow.split("pnpm install --frozen-lockfile").slice(1);
  assert.equal(buildJobs.length, 2);
  for (const job of buildJobs) {
    const contractBuild = job.indexOf("pnpm -C packages/industry-contract build");
    const webBuild = job.indexOf("pnpm -C apps/web build");
    assert.ok(contractBuild >= 0 && contractBuild < webBuild);
  }
});

test("投影在两个平台都生成并验收", () => {
  assert.ok((workflow.match(/pnpm projections:generate/g) ?? []).length >= 2);
  assert.ok((workflow.match(/pnpm projections:check/g) ?? []).length >= 2);
  assert.equal(packageJson.scripts["projections:generate"], "tsx scripts/generate-client-projections.mts --write");
  assert.equal(packageJson.scripts["projections:check"], "tsx scripts/generate-client-projections.mts");
});

test("三平台 unsigned 打包关闭自动证书发现并只在 signed 模式验签", () => {
  assert.ok((workflow.match(/CSC_IDENTITY_AUTO_DISCOVERY: "false"/g) ?? []).length >= 3);
  assert.doesNotMatch(workflow, /(?:CSC_LINK|CSC_KEY_PASSWORD|APPLE_ID|APPLE_APP_SPECIFIC_PASSWORD|APPLE_TEAM_ID|WIN_CSC_LINK|WIN_CSC_KEY_PASSWORD)\s*:\s*["']{2}/u);
  assert.ok((workflow.match(/retry\(\)/g) ?? []).length >= 6);
  assert.ok((workflow.match(/-c\.mac\.notarize=false/g) ?? []).length >= 2);
  assert.match(workflow, /if \[ "\$PLATFORM_SIGNING" = "signed" \]; then[\s\S]*codesign --verify --deep --strict[\s\S]*xcrun stapler validate/u);
  assert.match(workflow, /if \[ "\$PLATFORM_SIGNING" = "signed" \]; then[\s\S]*Get-AuthenticodeSignature/u);
});

test("Windows 冒烟隔离构建态服务端口并从同一临时根留存三段诊断", () => {
  for (const [name, port] of [
    ["WORKLOOM_PG_PORT", "55432"],
    ["WORKLOOM_SERVER_PORT", "58787"],
    ["WORKLOOM_WEB_PORT", "55173"],
    ["WORKLOOM_NATS_PORT", "54222"],
  ]) {
    assert.match(workflow, new RegExp(`${name}: ["']${port}["']`, "u"));
  }
  for (const rootName of ["wl-smoke", "wl-app-smoke", "wl-render-default"]) {
    assert.match(workflow, new RegExp(`RUNNER_TEMP/${rootName}`, "u"));
    assert.match(workflow, new RegExp(`runner\\.temp \\}\\}/${rootName}/logs`, "u"));
    assert.match(workflow, new RegExp(`runner\\.temp \\}\\}/${rootName}/install-state\\.json`, "u"));
  }
  assert.doesNotMatch(workflow, /\$\{TEMP\}\/wl-(?:smoke|app-smoke|render-default)/u);
  assert.match(workflow, /锁定源安装 17\.11\.0/u);
  assert.doesNotMatch(workflow, /锁定源安装 17\.2\.0/u);
});

test("产品身份、端口与固定下载资产名保持一致", () => {
  assert.equal(product.displayName, "老虎全球资产管理系统");
  assert.equal(product.desktop.portOffset, 610);
  assert.equal(product.release.appId, "com.geniusdapeng.workroomtiger");
  assert.equal(product.release.artifactPrefix, "Workroom.Tiger");
  assert.equal(product.release.workflow, ".github/workflows/build-desktop.yml");
  assert.match(builder, /productName: 老虎全球资产管理系统/u);
  assert.match(builder, /workloomPortOffset: 610/u);
  assert.match(builder, /artifactName: "Workroom\.Tiger-\$\{os\}-\$\{arch\}\.\$\{ext\}"/u);
  assert.match(verifier, /resolveDesktopWorkflowPath\(repositoryRoot, product\)/u);
});

test("单一发布器先封存候选，再以 Draft 原子发布五项资产并披露 unsigned 风险", () => {
  assert.ok((workflow.match(/desktop-release-finalizer\.mjs seal-platform/g) ?? []).length >= 2);
  assert.match(workflow, /--draft --latest=false/u);
  assert.match(workflow, /--draft=false --latest=true/u);
  assert.match(workflow, /isImmutable/u);
  assert.match(workflow, /未签名、未 Apple 公证/u);
  assert.match(workflow, /SmartScreen/u);
  assert.match(workflow, /WorkLoom-SHA512SUMS\.txt/u);
  assert.match(workflow, /WorkLoom-release-manifest\.json/u);
});

test("官网固定下载入口与真实 DMG 资产一致，不保留历史 ZIP 死链", () => {
  for (const site of [siteZh, siteEn]) {
    assert.doesNotMatch(site, /WorkLoom-macOS\.zip/u);
    assert.match(site, /releases\/latest\/download\/Workroom\.Tiger-mac-arm64\.dmg/u);
    assert.doesNotMatch(site, /Workroom%20Tiger-/u);
  }
});
