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
# 夜班 Quest 编排（22:00，WorkLoom 围栏自治驱动：自检→全链路→事件入库→官网发布）
NODE_BIN=$(dirname "$(command -v node)")
QCMD="cd $ROOT/governance && /usr/bin/env PATH=$NODE_BIN:/usr/local/bin:\$PATH TIGER_KERNEL_CMD='python3 main.py --mode daily --universe extended --top 30 --picks 8 --html --out reports' pnpm tsx --env-file=.env scripts/quest-trading-nightly.ts >> $LOG_DIR/quest.log 2>&1"
QLINE="0 22 * * * $QCMD"

# 三市编排（TIGER_QUEST_GOAL 选择模板；错峰避让）
CN_LINE="30 15 * * 1-5 cd $ROOT/governance && /usr/bin/env PATH=$NODE_BIN:/usr/local/bin:\$PATH TIGER_QUEST_GOAL='A股盘后' pnpm tsx --env-file=.env scripts/quest-trading-nightly.ts >> $LOG_DIR/quest-cn.log 2>&1"
HK_LINE="30 16 * * 1-5 cd $ROOT/governance && /usr/bin/env PATH=$NODE_BIN:/usr/local/bin:\$PATH TIGER_QUEST_GOAL='港股盘后' pnpm tsx --env-file=.env scripts/quest-trading-nightly.ts >> $LOG_DIR/quest-hk.log 2>&1"
US_INTRA_LINE="0 4 * * 2-6 cd $ROOT/governance && /usr/bin/env PATH=$NODE_BIN:/usr/local/bin:\$PATH TIGER_QUEST_GOAL='美股盘中' pnpm tsx --env-file=.env scripts/quest-trading-nightly.ts >> $LOG_DIR/quest-us-intra.log 2>&1"

( crontab -l 2>/dev/null | grep -v "ai-stock-trading-system\|main.py --universe full\|quest-trading-nightly" ; \
  echo "# ai-stock-trading-system 每日扫描（$(date +%F) 安装）" ; echo "$LINE" ; \
  echo "# tiger-trading 夜班 Quest 编排（22:00）" ; echo "$QLINE" ; \
  echo "# tiger-trading A股盘后编排（15:30 工作日）" ; echo "$CN_LINE" ; \
  echo "# tiger-trading 港股盘后编排（16:30 工作日）" ; echo "$HK_LINE" ; \
  echo "# tiger-trading 美股盘中编排（04:00 北京时间）" ; echo "$US_INTRA_LINE" ) | crontab -

echo "已安装每日调度：周一至周五 $RUN_AT 执行"
echo "命令: $CMD"
echo "日志: $LOG_DIR/cron.log ｜ 产物: $LOG_DIR/日报_*.html / result_*.json"
echo "查看: crontab -l ｜ 卸载: crontab -e 删除对应行"
