"""零基线纪律（每轮生产运行从干净的石板开始）。

用户硬性约束：每一轮全链路【从零开始】，绝不默认消费历史数据、脏数据、
上一轮生产残留。本模块是此纪律的唯一执行点：

  运行前必须清除（每轮重新获取）：
    cache/search/            搜索磁盘缓存（TTL 内会复用上一轮情报 → 必须清）
    cache/universe_full.json 全市场清单缓存（上一轮下载的股票池 → 必须清）

  白名单保留（会计台账，不进入决策输入）：
    reports/journal.json     信号账本——胜率追踪的本质是跨轮累计（先落账、
                             后按出场规则结算）。决策链路（pipeline/agents）
                             不读取账本，仅报告层追加"胜率追踪"章节展示。
    reports/sim_portfolio.json
                             小G模拟盘台账——公开验证战绩的本质是跨轮累计
                             （T+1 成交、出场纪律、净值曲线）。决策链路不读取
                             台账，仅报告层"小G模拟盘"页签展示。
    tuned_params.json        WFA 调优产物——保留落盘，但 pipeline 默认不加载
                             （use_tuned=False），仅显式 --use-tuned 才启用。

  从不写入、无残留的：
    行情 OHLCV（yahoo/stooq 实时拉取，进程内使用，不落盘）
    LLM 语义标注（内存中随结果输出，不缓存）
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger(__name__)

# 每轮必须清除的残留（相对仓库根 / 运行目录）
# v5.4：补充回测帧缓存——backtest.py 把全量 OHLCV 面板 pickle 到
# cache/frames_*.pkl，与"行情不落盘"的零基线约定矛盾；且缓存 key 不含
# 代码/指标版本，修复评分 bug 后当日仍会吃到旧帧。支持 * 通配。
PURGE_TARGETS = (
    os.path.join("cache", "search"),
    os.path.join("cache", "universe_full.json"),
    os.path.join("cache", "frames_*"),
)

# 白名单：即使位于被清理目录附近也绝不触碰（会计台账 / 显式启用的调优产物）
WHITELIST = (
    "journal.json",
    "sim_portfolio.json",
    "tuned_params.json",
)


def purge_run_state(base_dir: str = ".") -> dict:
    """清除上一轮运行残留。返回 {path: "removed"|"absent"|"kept(whitelist)"}。

    只清除 PURGE_TARGETS 列出的路径；白名单内文件即使同名也跳过。
    每轮生产运行（daily/premarket）在 pipeline 启动前调用。
    """
    report: dict[str, str] = {}
    import glob as _glob
    for rel in PURGE_TARGETS:
        path = os.path.join(base_dir, rel)
        name = os.path.basename(path)
        if name in WHITELIST:
            report[rel] = "kept(whitelist)"
            continue
        if "*" in rel:                      # 通配目标（如 cache/frames_*）
            matches = _glob.glob(path)
            for m in matches:
                if os.path.isdir(m):
                    shutil.rmtree(m, ignore_errors=True)
                elif os.path.isfile(m):
                    os.remove(m)
            if matches:
                logger.info("零基线: 清除 %s（%d 个匹配）", rel, len(matches))
            report[rel] = f"removed({len(matches)} files)" if matches else "absent"
            continue
        if os.path.isdir(path):
            n = len(os.listdir(path))
            shutil.rmtree(path, ignore_errors=True)
            report[rel] = f"removed({n} files)"
            logger.info("零基线: 清除 %s（%d 个文件）", rel, n)
        elif os.path.isfile(path):
            os.remove(path)
            report[rel] = "removed(1 files)"
            logger.info("零基线: 清除 %s", rel)
        else:
            report[rel] = "absent"
    return report
