#!/usr/bin/env bash
# 老虎交易 · 全栈一键启动（交易内核 + WorkLoom 治理底座）
# 前置：Node ≥24、pnpm 10、Docker（PostgreSQL 17 容器）
# 用法：bash scripts/stack_setup.sh
set -euo pipefail
cd "$(dirname "$0")/../governance"

echo "==> [1/6] 环境检查"
node --version | grep -E "v2[4-9]|v[3-9]" >/dev/null || { echo "需要 Node ≥24（当前 $(node --version)）"; exit 1; }
docker ps >/dev/null 2>&1 || { echo "Docker 不可用"; exit 1; }
pnpm --version >/dev/null

echo "==> [2/6] 安装依赖"
pnpm install

echo "==> [3/6] 启动 PostgreSQL 并迁移"
docker compose up -d
sleep 5
[ -f .env ] || cp .env.example .env
pnpm db:migrate

echo "==> [4/6] 种子（演示租户 + 老虎交易 trading bundle）"
pnpm db:seed
pnpm tsx --env-file=.env scripts/seed-trading.ts

echo "==> [5/6] 内核事件入库（若存在 governance_events.jsonl）"
if [ -f ../reports/governance_events.jsonl ]; then
  pnpm tsx --env-file=.env scripts/ingest-tiger-events.ts
else
  echo "    （无内核事件文件，跳过——先运行 main.py 产生）"
fi

echo "==> [6/6] 启动服务"
echo "    后端:  pnpm -C apps/server dev   → http://localhost:8787"
echo "    前端:  pnpm -C apps/web dev      → http://localhost:5173"
echo "    （或 pnpm dev 同时起两端）"

cat <<'TIP'

全栈就绪：
  - trading bundle：33 数字员工 / 18 围栏 / 6 技能 / 7 触发器（三市时段+夜班+月度WFA）
  - 事件入库：内核每次运行后执行 pnpm tsx --env-file=.env scripts/ingest-tiger-events.ts
  - 验链：   pnpm tsx scripts/verify-chain.ts
TIP
