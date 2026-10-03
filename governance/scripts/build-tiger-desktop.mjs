#!/usr/bin/env node
/** Build an isolated desktop candidate or a signed native release from the final payload. */
import { createHash, createPrivateKey } from "node:crypto";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import YAML from "yaml";
import { assertKernelPayload, platformTarget } from "./pack-tiger-kernel.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const sha = (data) => createHash("sha256").update(data).digest("hex");
const arg = (name, fallback) => { const i = process.argv.indexOf(name); return i < 0 ? fallback : process.argv[i + 1]; };

export function buildTarPlan(payload, archive, pathApi = { dirname, basename, relative, isAbsolute }) {
  const cwd = pathApi.dirname(archive);
  const payloadArg = pathApi.relative(cwd, payload);
  if (pathApi.isAbsolute(payloadArg)) throw new Error("载荷与构建输出必须位于同一个卷");
  return { cwd, args: ["-czf", pathApi.basename(archive), "-C", payloadArg.replaceAll("\\", "/") || ".", "."] };
}

/** Sign first, re-index the changed bundle manifests, then verify the bytes in the actual tar. */
export function archiveSignedPayload({ payload, archive, productId, version, productManifestSha256, signBundles,
  tar = process.platform === "win32" ? join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe") : "/usr/bin/tar" }) {
  rmSync(archive, { force: true });
  let checkRoot;
  try {
    if (typeof signBundles !== "function") throw new Error("行业包签名步骤缺失");
    const { generatePayloadIntegrity, verifyPayloadIntegrity } = require("../apps/desktop/electron/payload-integrity.cjs");
    const expected = { expectedProductId: productId, expectedVersion: version };
    const before = verifyPayloadIntegrity(payload, expected);
    if (before.productManifestSha256 !== productManifestSha256) throw new Error("载荷产品清单与当前构建源不一致");
    const original = JSON.parse(readFileSync(join(payload, "payload-integrity.json"), "utf8"));
    signBundles();
    const sealed = generatePayloadIntegrity(payload, expected);
    const signed = JSON.parse(readFileSync(join(payload, "payload-integrity.json"), "utf8"));
    if (sealed.productManifestSha256 !== productManifestSha256 || JSON.stringify(original.links) !== JSON.stringify(signed.links)
        || original.files.length !== signed.files.length || original.files.some((entry, index) => {
          const current = signed.files[index];
          return current?.path !== entry.path || !/^runtime\/bundles\/[^/]+\/bundle\.json$/u.test(entry.path)
            && (current.bytes !== entry.bytes || current.sha256 !== entry.sha256);
        })) throw new Error("行业包签名改变了允许范围之外的不可变载荷");
    mkdirSync(dirname(archive), { recursive: true });
    const plan = buildTarPlan(payload, archive);
    execFileSync(tar, plan.args, { cwd: plan.cwd, stdio: "inherit", timeout: 300000 });
    checkRoot = mkdtempSync(join(dirname(archive), ".tiger-archive-check-"));
    const relativeArchive = relative(checkRoot, archive);
    if (isAbsolute(relativeArchive)) throw new Error("载荷归档与验真目录必须在同一个卷");
    execFileSync(tar, ["-xzf", relativeArchive.replaceAll("\\", "/")], { cwd: checkRoot, stdio: "inherit", timeout: 300000 });
    const actual = verifyPayloadIntegrity(checkRoot, expected);
    if (JSON.stringify(actual) !== JSON.stringify(sealed)) throw new Error("最终归档载荷与签名后索引不一致");
    return { payloadIntegrity: actual, payloadSha256: sha(readFileSync(archive)), signedArchiveVerified: true };
  } catch (error) {
    rmSync(archive, { force: true });
    throw error;
  } finally { if (checkRoot) rmSync(checkRoot, { recursive: true, force: true }); }
}

export function releasePolicy({ platform, candidate, environment = process.env, hostPlatform = process.platform, hostArch = process.arch }) {
  const native = platform === "mac" ? hostPlatform === "darwin" && hostArch === "arm64"
    : platform === "win" ? hostPlatform === "win32" && hostArch === "x64" : false;
  if (!native) throw new Error("桌面构建和验收必须在目标原生系统执行");
  if (!environment.BUNDLE_SIGNING_PRIVATE_KEY || !environment.BUNDLE_SIGNING_KEY_ID) {
    throw new Error("缺少行业包 Ed25519 签名凭据；候选包也不能跳过内部验签");
  }
  if (createPrivateKey(environment.BUNDLE_SIGNING_PRIVATE_KEY).asymmetricKeyType !== "ed25519") throw new Error("行业包签名密钥必须为 Ed25519");
  if (!candidate) {
    if (!(environment.CSC_LINK && environment.CSC_KEY_PASSWORD)) throw new Error("正式客户端缺少平台签名证书和密码");
    if (platform === "mac" && !((environment.APPLE_API_KEY && environment.APPLE_API_KEY_ID && environment.APPLE_API_ISSUER)
        || (environment.APPLE_ID && environment.APPLE_APP_SPECIFIC_PASSWORD && environment.APPLE_TEAM_ID))) {
      throw new Error("正式 Mac 客户端缺少 Apple 公证凭据");
    }
  }
  return { native: true, candidate, unsignedPlatform: candidate };
}

export function buildDesktop({ platform, candidate, version, payloadRoot, outputRoot, keyFile, keyIdFile, target }) {
  const arch = platform === "mac" ? "arm64" : "x64";
  const payload = resolve(payloadRoot);
  const output = resolve(outputRoot);
  if (!/^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$/u.test(version)) throw new Error("必须提供明确的语义版本号");
  if (!existsSync(join(payload, "PAYLOAD_VERSION"))) throw new Error("最终载荷缺少 PAYLOAD_VERSION");
  if (readFileSync(join(payload, "PAYLOAD_VERSION"), "utf8").trim() !== version) throw new Error("构建版本与载荷版本不一致");
  const environment = { ...process.env };
  if (keyFile || keyIdFile) {
    if (!candidate || !keyFile || !keyIdFile) throw new Error("本机密钥文件只允许明确的隔离候选构建，并必须同时指定 key-id 文件");
    environment.BUNDLE_SIGNING_PRIVATE_KEY = readFileSync(resolve(keyFile), "utf8");
    environment.BUNDLE_SIGNING_KEY_ID = readFileSync(resolve(keyIdFile), "utf8").trim();
  }
  const policy = releasePolicy({ platform, candidate, environment });
  const kernel = assertKernelPayload(payload, platformTarget(platform, arch));
  mkdirSync(output, { recursive: true });
  rmSync(join(output, "desktop-build.json"), { force: true });
  const appStage = join(output, "app-source");
  if (existsSync(appStage)) rmSync(appStage, { recursive: true });
  mkdirSync(join(appStage, "apps", "desktop"), { recursive: true });
  cpSync(join(ROOT, "apps", "desktop", "electron"), join(appStage, "apps", "desktop", "electron"), { recursive: true });
  const sourcePackage = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));
  const manifest = JSON.parse(readFileSync(join(ROOT, "product.manifest.json"), "utf8"));
  const electronVersion = JSON.parse(readFileSync(join(ROOT, "node_modules", "electron", "package.json"), "utf8")).version;
  const trust = join(output, "bundle-trust.json");
  const archive = join(output, "payload.tar.gz");
  const signedArchive = archiveSignedPayload({ payload, archive, productId: manifest.productId, version,
    productManifestSha256: sha(readFileSync(join(ROOT, "product.manifest.json"))),
    signBundles: () => execFileSync(process.execPath, ["--import", "tsx", join(ROOT, "scripts", "bundle-governance.mts"), "--refresh-digests", "--release"], {
      cwd: ROOT, env: { ...environment, BUNDLES_ROOT: join(payload, "runtime", "bundles"), BUNDLE_TRUST_OUTPUT: trust }, stdio: "inherit", timeout: 120000,
    }),
  });
  const configured = YAML.parse(readFileSync(join(ROOT, "electron-builder.yml"), "utf8"));
  const config = { ...configured,
    directories: { output: join(output, "artifacts"), buildResources: join(ROOT, "apps", "desktop", "electron", "assets") },
    files: ["apps/desktop/electron/**/*", "package.json"], electronVersion, npmRebuild: false,
    extraResources: [{ from: trust, to: "bundle-trust.json" }, { from: archive, to: "payload.tar.gz" },
      { from: join(payload, "PAYLOAD_VERSION"), to: "payload/PAYLOAD_VERSION" }],
    forceCodeSigning: !candidate,
    extraMetadata: { ...configured.extraMetadata, version, workloomReleaseChannel: candidate ? "candidate" : "stable" },
    artifactName: candidate ? "Workroom.Tiger-candidate-${os}-${arch}.${ext}" : configured.artifactName,
    mac: { ...configured.mac, icon: join(ROOT, configured.mac.icon), entitlements: join(ROOT, configured.mac.entitlements),
      entitlementsInherit: join(ROOT, configured.mac.entitlementsInherit), notarize: !candidate, ...(candidate ? { identity: null } : {}) },
    win: { ...configured.win, icon: join(ROOT, configured.win.icon) },
  };
  writeFileSync(join(appStage, "package.json"), JSON.stringify({ name: manifest.productId, version, private: true,
    main: configured.extraMetadata.main, description: manifest.displayName, author: "WorkLoom", productName: manifest.displayName,
    workloomPortOffset: manifest.desktop.portOffset, workloomReleaseChannel: candidate ? "candidate" : "stable" }, null, 2) + "\n");
  const configFile = join(output, "electron-builder.json");
  writeFileSync(configFile, JSON.stringify(config, null, 2) + "\n");
  const builder = [join(ROOT, "node_modules", "electron-builder", "cli.js"), "--projectDir", appStage,
    "--config", configFile, "--publish", "never", platform === "mac" ? "--mac" : "--win", ...(target === "dir" ? [] : [target]), `--${arch}`, ...(target === "dir" ? ["--dir"] : [])];
  const builderEnvironment = { ...environment, ...(candidate ? { CSC_IDENTITY_AUTO_DISCOVERY: "false" } : {}) };
  execFileSync(process.execPath, builder, { cwd: ROOT, env: builderEnvironment, stdio: "inherit", timeout: 600000 });
  const app = platform === "mac" ? join(output, "artifacts", "mac-arm64", `${manifest.displayName}.app`)
    : join(output, "artifacts", "win-unpacked");
  const resources = platform === "mac" ? join(app, "Contents", "Resources") : join(app, "resources");
  if (!existsSync(join(resources, "payload.tar.gz")) || sha(readFileSync(join(resources, "payload.tar.gz"))) !== sha(readFileSync(archive))) {
    throw new Error("最终 App 资源摘要与已签行业载荷不一致");
  }
  const executable = platform === "mac" ? join(app, "Contents", "MacOS", manifest.displayName) : join(app, `${manifest.displayName}.exe`);
  if (!existsSync(executable)) throw new Error("最终客户端可执行文件缺失");
  const receipt = { schemaVersion: "tiger.desktop-build/v1", platform, arch, version, ...policy,
    productId: manifest.productId, app, resources, executable, electronVersion,
    ...signedArchive, trustSha256: sha(readFileSync(trust)), kernel,
    lockSha256: sha(readFileSync(join(ROOT, "pnpm-lock.yaml"))), packageManager: sourcePackage.packageManager,
    builtAt: new Date().toISOString(), smokeVerified: false };
  writeFileSync(join(output, "desktop-build.json"), JSON.stringify(receipt, null, 2) + "\n");
  return receipt;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const platform = arg("--platform", process.platform === "darwin" ? "mac" : "win");
    const receipt = buildDesktop({ platform, candidate: process.argv.includes("--candidate"), version: arg("--version"),
      payloadRoot: arg("--payload", join(ROOT, "dist-payload")), outputRoot: arg("--output", join(ROOT, "release", "tiger-desktop")),
      keyFile: arg("--bundle-key-file"), keyIdFile: arg("--bundle-key-id-file"), target: arg("--target", platform === "mac" ? "dmg" : "nsis") });
    console.log(`Tiger 桌面构建完成：${JSON.stringify(receipt)}`);
  } catch (error) { console.error(error instanceof Error ? error.message : String(error)); process.exitCode = 1; }
}
