#!/usr/bin/env bash
# 老虎交易 · 一键安装（交易内核路径）
# 用法：bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> [1/4] Python 版本检查（需要 3.10+）"
python3 -c "import sys; assert sys.version_info >= (3, 10), sys.version; print('    python', sys.version.split()[0])"

echo "==> [2/4] 安装依赖"
if python3 -m venv .venv 2>/dev/null; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  echo "    已创建虚拟环境 .venv"
fi
pip install -q -r requirements.txt
echo "    依赖安装完成"

echo "==> [3/4] 冒烟测试（离线 demo，确定性夹具）"
python3 -m pytest tests/ -q --ignore=tests/test_provider_official.py -x -k "not smoke" 2>&1 | tail -1

echo "==> [4/4] 自检（网络/数据源/LLM）"
bash scripts/doctor.sh || true

cat <<'TIP'

安装完成。下一步：
  离线演示：  python3 main.py --demo
  真实数据：  python3 main.py --mode daily --universe extended --top 30 --picks 8 --html
  点亮 LLM：  见 docs/QUICKSTART.md「LLM 配置」（叙事/辩论/语义清洗需要）
  官网发布：  bash scripts/publish_site.sh（日报 → site/index.html）
TIP
