#!/usr/bin/env bash
# 每日自动扫描调度安装器（本地 crontab 方案）
# 用法: bash scripts/install_cron.sh [HH:MM]   默认 06:00（北京时间，美股收盘后）
set -euo pipefail

RUN_AT="${1:-06:00}"
HOUR="${RUN_AT%%:*}"; MIN="${RUN_AT##*:}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/reports"
mkdir -p "$LOG_DIR"

# 周一至周五（美股交易日近似）；真实行情用 --universe full
CMD="cd $ROOT && /usr/bin/env python3 main.py --universe full --top 30 --picks 8 \
>> $LOG_DIR/cron.log 2>&1"

LINE="$MIN $HOUR * * 1-5 $CMD"
( crontab -l 2>/dev/null | grep -v "ai-stock-trading-system\|main.py --universe full" ; \
  echo "# ai-stock-trading-system 每日扫描（$(date +%F) 安装）" ; echo "$LINE" ) | crontab -

echo "已安装每日调度：周一至周五 $RUN_AT 执行"
echo "命令: $CMD"
echo "日志: $LOG_DIR/cron.log ｜ 产物: $LOG_DIR/日报_*.html / result_*.json"
echo "查看: crontab -l ｜ 卸载: crontab -e 删除对应行"
