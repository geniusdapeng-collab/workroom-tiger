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
from pathlib import Path

logger = logging.getLogger(__name__)

# 每轮必须清除的残留（相对仓库根 / 运行目录）
# v5.4：补充回测帧缓存——backtest.py 把全量 OHLCV 面板 pickle 到
# cache/frames_*.pkl，与"行情不落盘"的零基线约定矛盾；且缓存 key 不含
# 代码/指标版本，修复评分 bug 后当日仍会吃到旧帧。支持 * 通配。
PURGE_TARGETS = (
    os.path.join("cache", "search"),
    os.path.join("cache", "universe_full.json"),
    os.path.join("cache", "universe_cn.json"),
    os.path.join("cache", "universe_hk.json"),
    os.path.join("cache", "frames_*"),
    # v6.3 S3：可信度/交叉验证缓存的指定落盘位置（当前实现为内存态、随进程
    # 结束消亡；一旦未来落盘必须放在本目录，零基线每轮必清，绝不跨轮复用）
    os.path.join("cache", "credibility"),
)

# 白名单：即使位于被清理目录附近也绝不触碰（会计台账 / 显式启用的调优产物）
WHITELIST = (
    "journal.json",
    "sim_portfolio.json",
    "tuned_params.json",
    "calibration_samples.json",   # v6.3：校准样本库（与 journal 同级会计账，不进决策输入）
    "governance_events.jsonl",    # S5：治理五元事件账（哈希链留痕旁路，跨轮累计，不进决策输入）
)


# Approval evidence and options samples are persistent accounting artifacts.
# Their subtrees are preserved even if a caller put them inside a cache target.
WHITELIST_DIRS = ("review_proposals", "review_executions", "options_hist")


def run_directory(base_dir: str | Path) -> Path:
    """Validate the selected namespace without creating or following a symlink."""
    root = Path(os.path.abspath(base_dir))
    for component in (root, *root.parents):
        if component.is_symlink():
            raise ValueError(f"Run directory cannot contain a symlink: {component}")
    if root.exists() and not root.is_dir():
        raise ValueError(f"Run directory must be a directory: {root}")
    return root


def run_path(base_dir: str | Path, *parts: str) -> Path:
    """Resolve a path owned by one run, rejecting traversal and symlink aliases."""
    root = run_directory(base_dir)
    path = Path(os.path.abspath(root.joinpath(*parts)))
    if not path.is_relative_to(root):
        raise ValueError(f"Path escapes the selected run directory: {path}")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"Run path cannot contain a symlink: {component}")
        if component == root:
            break
    return path


def _protected(path: Path) -> bool:
    return (path.name in WHITELIST or path.name in WHITELIST_DIRS
            or path.name.endswith(".lock")
            and path.name[:-5] in (*WHITELIST, ".review-state"))


def purge_run_state(base_dir: str = ".") -> dict:
    """Remove volatile inputs in one namespace, preserving accounting evidence.

    Validate every target before deletion. Unexpected symlinks or I/O failures
    are explicit failures; a failed deletion must never be reported as success.
    """
    root = run_directory(base_dir)
    targets = []
    for relative in PURGE_TARGETS:
        if "*" in relative:
            parent, pattern = Path(relative).parent, Path(relative).name
            directory = run_path(root, str(parent))
            matches = sorted(directory.glob(pattern)) if directory.exists() else []
        else:
            target = run_path(root, relative)
            matches = [target] if target.exists() else []
        targets.append((relative, matches))

    def preflight(path):
        if _protected(path):
            return
        run_path(root, str(path.relative_to(root)))
        if path.is_dir():
            for child in sorted(path.iterdir()):
                preflight(child)

    for _, paths in targets:
        for path in paths:
            preflight(path)

    def remove(path):
        if _protected(path):
            return 0, True
        run_path(root, str(path.relative_to(root)))
        if path.is_dir():
            removed, kept = 0, False
            for child in sorted(path.iterdir()):
                count, preserved = remove(child)
                removed += count
                kept = kept or preserved
            if not kept:
                path.rmdir()
            return removed, kept
        path.unlink()
        return 1, False

    report = {}
    for relative, paths in targets:
        removed, kept = 0, False
        for path in paths:
            count, preserved = remove(path)
            removed += count
            kept = kept or preserved
        if kept:
            status = f"removed({removed} files); kept(whitelist)"
        else:
            status = f"removed({removed} files)" if paths else "absent"
        report[relative] = status
        logger.info("Zero baseline: %s %s", relative, status)
    return report
