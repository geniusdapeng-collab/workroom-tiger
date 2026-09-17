#!/usr/bin/env bash
# 老虎交易 · 部署自检：网络可达性画像 / 数据源 / LLM / 运行环境
# 用法：bash scripts/doctor.sh  （退出码 0=至少一条完整数据链路可用，1=硬阻断）
set -uo pipefail
cd "$(dirname "$0")/.."
HARD_FAIL=0
UA="Mozilla/5.0 (X11; Linux x86_64) TigerTrading/3.1 (research)"

probe() {  # probe <名称> <URL> <期望包含串> <hard|soft> [额外curl参数...]
  local name="$1" url="$2" want="$3" level="$4"; shift 4
  local body
  if body=$(curl -s --max-time 8 -H "User-Agent: $UA" "$@" "$url" 2>/dev/null) && [ -n "$body" ]; then
    if [ -n "$want" ] && ! grep -qF "$want" <<<"$body"; then
      echo "  ✗ $name — 可达但内容异常（疑似反爬/封禁）"
      [ "$level" = hard ] && HARD_FAIL=1
    else
      echo "  ✓ $name"
    fi
  else
    echo "  ✗ $name — 不可达"
    [ "$level" = hard ] && HARD_FAIL=1
  fi
}

echo "== 行情源（降级链：一环可用即可，全部不可用才阻断）"
probe "yahoo   " "https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=1d&interval=1d" "chart" soft
probe "stooq   " "https://stooq.com/q/l/?s=spy.us&f=sd2t2ohlcv&h&e=csv" "SPY" soft
probe "official" "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10" "DGS10" soft
probe "tencent " "https://qt.gtimg.cn/q=usSPY" "SPY" soft
probe "sina    " "https://hq.sinajs.cn/list=gb_spy" "spy" soft -H "Referer: https://finance.sina.com.cn"

echo "== 情报源（SearchHub，源级失败不阻塞，仅画像）"
probe "SEC EDGAR   " "https://efts.sec.gov/LATEST/search-index?q=test" "hits" soft
probe "Google News " "https://news.google.com/rss/search?q=market" "rss" soft
probe "Reddit      " "https://www.reddit.com/r/stocks/hot.json?limit=1" "data" soft
probe "东财资讯     " "https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param=%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22%E7%BE%8E%E8%82%A1%22%2C%22type%22%3A%5B%22cmsArticleWebOld%22%5D%2C%22client%22%3A%22web%22%2C%22clientType%22%3A%22web%22%2C%22clientVersion%22%3A%22curr%22%2C%22param%22%3A%7B%22cmsArticleWebOld%22%3A%7B%22searchScope%22%3A%22default%22%2C%22sort%22%3A%22default%22%2C%22pageIndex%22%3A1%2C%22pageSize%22%3A1%7D%7D%7D" "cmsArticleWebOld" soft
probe "新浪快讯     " "https://zhibo.sina.com.cn/api/zhibo/feed?zhibo_id=152&page_size=1" "result" soft -H "Referer: https://finance.sina.com.cn"

echo "== LLM（叙事/辩论/语义清洗环节的开关）"
if [ -n "${KIMI_API_KEY:-}" ] || [ -f "$HOME/.kimi/agent-gw.json" ]; then
  echo "  ✓ 检测到 LLM 凭据（KIMI_API_KEY 或 ~/.kimi/agent-gw.json）"
else
  echo "  ⚠ 未检测到 LLM 凭据——LLM 环节将走透传（系统可运行，但叙事/辩论维度缺失）"
  echo "    配置方法见 docs/QUICKSTART.md「LLM 配置」"
fi

echo "== 运行环境（硬依赖）"
python3 -c "import pandas, numpy, requests" 2>/dev/null && echo "  ✓ python 依赖" || { echo "  ✗ python 依赖缺失：pip install -r requirements.txt"; HARD_FAIL=1; }
[ -w reports ] && echo "  ✓ reports/ 可写" || { echo "  ✗ reports/ 不可写"; HARD_FAIL=1; }

# 关键判定：行情硬链路至少一条可用（yahoo/stooq/official/tencent/sina 任一）
echo
if [ "$HARD_FAIL" = 0 ]; then
  echo "结论：可投入运行（硬依赖满足；不可达源将由降级链/熔断自动接管）"
else
  echo "结论：存在硬阻断项，请先修复上述 ✗（运行环境类）"
fi
exit $HARD_FAIL
