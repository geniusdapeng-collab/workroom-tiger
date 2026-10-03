# Native Windows x64 build gate. Requires a clean CI runner with MSVC and Chocolatey.
param(
  [Parameter(Mandatory=$true)][string]$Version,
  [switch]$Candidate
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
if ([Environment]::OSVersion.Platform -ne "Win32NT" -or -not [Environment]::Is64BitProcess) {
  throw "必须在 Windows x64 原生构建进程执行"
}
foreach ($tool in @("node", "npm", "git", "choco")) {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "构建机缺少 $tool（客户机无需安装）" }
}
function Invoke-Checked([string]$Command, [string[]]$Arguments) {
  & $Command @Arguments
  if ($LASTEXITCODE -ne 0) { throw "$Command 执行失败（exit=$LASTEXITCODE）" }
}
Invoke-Checked "node" @("-e", 'if(process.arch!=="x64"||process.versions.node!=="24.19.0")throw new Error("原生构建需要锁定 Node24.19.0 x64（含 npm11.17.0）")')
# Fail before package installation when required signing secrets are absent.
Invoke-Checked "node" @("--input-type=module", "-e", 'import {createPrivateKey} from "node:crypto";if(!process.env.BUNDLE_SIGNING_PRIVATE_KEY||!process.env.BUNDLE_SIGNING_KEY_ID)throw new Error("缺少行业包签名凭据");if(createPrivateKey(process.env.BUNDLE_SIGNING_PRIVATE_KEY).asymmetricKeyType!=="ed25519")throw new Error("行业包密钥类型错误")')
if (-not $Candidate -and (-not $env:CSC_LINK -or -not $env:CSC_KEY_PASSWORD)) { throw "缺少正式 Windows 签名证书" }
$bash = Join-Path $env:ProgramFiles "Git\bin\bash.exe"
if (-not (Test-Path $bash)) { throw "构建机缺少 Git for Windows Bash；客户机不需要它" }
Invoke-Checked "npm" @("exec", "--yes", "--package=pnpm@10.14.0", "--", "pnpm", "install", "--frozen-lockfile")
Invoke-Checked "npm" @("exec", "--yes", "--package=pnpm@10.14.0", "--", "pnpm", "-C", "packages/industry-contract", "build")
Invoke-Checked "npm" @("exec", "--yes", "--package=pnpm@10.14.0", "--", "pnpm", "projections:check")
Invoke-Checked "node" @("scripts/verify-product-content.mjs")
Invoke-Checked "npm" @("exec", "--yes", "--package=pnpm@10.14.0", "--", "pnpm", "-C", "apps/web", "build")
Invoke-Checked "node" @("--test", "apps/desktop/electron/bootstrap.test.cjs", "apps/desktop/electron/desktop-safety.test.cjs", "apps/desktop/electron/industry-runtime.test.cjs", "scripts/tiger-desktop-delivery.test.mjs", "scripts/desktop-workflow-path.test.mjs", "scripts/release-assets.test.mjs")
Invoke-Checked "node" @("--import", "tsx", "--test", "scripts/proposal-bridge.test.mjs")
& "$root\scripts\build-pgvector-win.ps1"
Invoke-Checked $bash @("scripts/pack-electron-payload.sh", "--platform", "win", "--arch", "x64", "--version", $Version)
$buildRoot = if ($env:TIGER_DESKTOP_BUILD_ROOT) { $env:TIGER_DESKTOP_BUILD_ROOT } else { Join-Path $root "release\tiger-native-win" }
$buildArgs = @("scripts/build-tiger-desktop.mjs", "--platform", "win", "--version", $Version, "--payload", (Join-Path $root "dist-payload"), "--output", $buildRoot)
if ($Candidate) { $buildArgs += "--candidate" }
Invoke-Checked "node" $buildArgs
Invoke-Checked "node" @("scripts/tiger-desktop-smoke.mjs", "--build", (Join-Path $buildRoot "desktop-build.json"), "--output", (Join-Path $buildRoot "smoke"), "--render")
if (-not $Candidate) {
  $installers = @(Get-ChildItem (Join-Path $buildRoot "artifacts") -File -Filter "Workroom.Tiger-win-x64.exe")
  if ($installers.Count -ne 1) { throw "正式 NSIS 安装包数量异常" }
  $signature = Get-AuthenticodeSignature $installers[0].FullName
  if ($signature.Status -ne "Valid") { throw "正式 NSIS 安装包签名无效：$($signature.Status)" }
}
Invoke-Checked "node" @("-e", 'const f=require("node:fs");const b=JSON.parse(f.readFileSync(process.argv[1],"utf8"));const s=JSON.parse(f.readFileSync(b.smokeReceipt,"utf8"));if(!b.smokeVerified||!s.finalAppVerified||!s.emptyPath||s.appChecks.length!==3)throw new Error("原生最终资源验收回执不完整");console.log("Tiger Windows 原生桌面门禁通过：",b.candidate?"候选（平台未签名）":"正式签名")', (Join-Path $buildRoot "desktop-build.json"))
