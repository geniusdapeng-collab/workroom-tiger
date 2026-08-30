#!/usr/bin/env bash
# 老虎交易 · 官网发布：把最新日报发布到 site/（闭合"日报→官网"发布环）
# 用法：bash scripts/publish_site.sh [日报HTML路径]
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="${1:-}"
if [ -z "$SRC" ]; then
  SRC=$(ls -t reports/日报_*.html 2>/dev/null | head -1 || true)
fi
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  echo "未找到日报 HTML（reports/日报_*.html），请先运行 main.py --html" >&2
  exit 1
fi

cp "$SRC" site/index.html
echo "已发布 $SRC → site/index.html"
echo "提示：复盘纪要（reports/复盘_*.md）与治理事件（reports/governance_events.jsonl）"
echo "      可由 site/governance.html 责任界面直接读取（同目录部署时）。"
