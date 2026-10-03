#!/usr/bin/env bash
# Native Mac M build gate. This script is a builder-side workflow, never a customer prerequisite.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${1:?用法：build-tiger-desktop-native.sh VERSION [--candidate]}"
MODE="${2:-}"
[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || { echo "必须在 Mac M 芯片原生执行" >&2; exit 1; }
[ -z "$MODE" ] || [ "$MODE" = "--candidate" ] || { echo "未知构建模式：$MODE" >&2; exit 1; }
command -v node >/dev/null || { echo "构建机缺少 Node（客户机无需安装）" >&2; exit 1; }
command -v npm >/dev/null || { echo "构建机缺少 npm（客户机无需安装）" >&2; exit 1; }
node -e 'if(process.versions.node!=="24.19.0")throw new Error("原生构建需要锁定 Node24.19.0（含 npm11.17.0）")'
# Validate signing before downloading/installing or building any large asset.
node --input-type=module -e 'import {createPrivateKey} from "node:crypto";if(!process.env.BUNDLE_SIGNING_PRIVATE_KEY||!process.env.BUNDLE_SIGNING_KEY_ID)throw new Error("缺少行业包签名凭据");if(createPrivateKey(process.env.BUNDLE_SIGNING_PRIVATE_KEY).asymmetricKeyType!=="ed25519")throw new Error("行业包密钥类型错误")'
if [ "$MODE" != "--candidate" ]; then
  [ -n "${CSC_LINK:-}" ] && [ -n "${CSC_KEY_PASSWORD:-}" ] || { echo "缺少正式 Mac 签名证书" >&2; exit 1; }
  if ! { [ -n "${APPLE_API_KEY:-}" ] && [ -n "${APPLE_API_KEY_ID:-}" ] && [ -n "${APPLE_API_ISSUER:-}" ]; } \
    && ! { [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ]; }; then
    echo "缺少正式 Mac 公证凭据" >&2; exit 1
  fi
fi
npm exec --yes --package=pnpm@10.14.0 -- pnpm install --frozen-lockfile
npm exec --yes --package=pnpm@10.14.0 -- pnpm -C packages/industry-contract build
npm exec --yes --package=pnpm@10.14.0 -- pnpm projections:check
node scripts/verify-product-content.mjs
npm exec --yes --package=pnpm@10.14.0 -- pnpm -C apps/web build
node --test apps/desktop/electron/bootstrap.test.cjs apps/desktop/electron/desktop-safety.test.cjs apps/desktop/electron/industry-runtime.test.cjs scripts/tiger-desktop-delivery.test.mjs scripts/desktop-workflow-path.test.mjs scripts/release-assets.test.mjs
node --import tsx --test scripts/proposal-bridge.test.mjs
bash scripts/pack-electron-payload.sh --platform mac --arch arm64 --version "$VERSION"
BUILD_ROOT="${TIGER_DESKTOP_BUILD_ROOT:-$PWD/release/tiger-native-mac}"
BUILD_ARGS=(--platform mac --version "$VERSION" --payload "$PWD/dist-payload" --output "$BUILD_ROOT")
[ -z "$MODE" ] || BUILD_ARGS+=("$MODE")
node scripts/build-tiger-desktop.mjs "${BUILD_ARGS[@]}"
node scripts/tiger-desktop-smoke.mjs --build "$BUILD_ROOT/desktop-build.json" --output "$BUILD_ROOT/smoke" --render
if [ "$MODE" != "--candidate" ]; then
  APP_PATH="$(node -e 'process.stdout.write(JSON.parse(require("node:fs").readFileSync(process.argv[1],"utf8")).app)' "$BUILD_ROOT/desktop-build.json")"
  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
  spctl --assess --type execute --verbose=2 "$APP_PATH"
  xcrun stapler validate "$APP_PATH"
fi
node -e 'const f=require("node:fs");const p=process.argv[1];const build=JSON.parse(f.readFileSync(p,"utf8"));const smoke=JSON.parse(f.readFileSync(build.smokeReceipt,"utf8"));if(!build.smokeVerified||!smoke.finalAppVerified||!smoke.emptyPath||smoke.appChecks.length!==3)throw new Error("原生最终资源验收回执不完整");console.log("Tiger Mac 原生桌面门禁通过：",build.candidate?"候选（平台未签名）":"正式签名+公证")' "$BUILD_ROOT/desktop-build.json"
