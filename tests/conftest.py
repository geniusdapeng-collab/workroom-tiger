"""pytest 全局夹具：测试运行绝不写产品台账（v6.5 修复）。

根因（T-2026-0929-0002 排雷实证）：期权历史库 `OptionsHistoryStore` 的默认
落盘 root 是 `config.REPORTS_DIR/options_hist`——那是随仓库提交的公开台账，
积累 ≥10 条后进入真实决策（TSS 期权维度分位）。测试夹具用 DemoProvider 子类
并改 `name`（如 `onestale-demo`）时，旧守卫（`provider.name == "demo"`）被绕过，
跑一遍 `pytest` 就会往 `reports/options_hist/` 写入 10 个合成样本
（实测 2026-09-29：ADBE/AI/ASTS/AVGO/BWXT/CEG/DELL/IBM/KLAC/NOVA 各 +1 条）。

本夹具把默认 root 重定向到临时目录，从机制上断开"测试 → 产品台账"的写路径：
即使某条测试忘了注入 store，也只会写进 tmp，不会污染真实分位历史。
默认校准 journal 与样本路径也指向该测试目录，防止 pipeline 读写产品会计账。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_product_ledgers(tmp_path_factory, monkeypatch):
    """把期权历史与校准会计账的默认路径指向每个测试的临时目录。"""
    from trading_system import config
    ledger_root = tmp_path_factory.mktemp("options-hist")
    monkeypatch.setenv("TS_OPTIONS_HIST_DIR", str(ledger_root))
    monkeypatch.setattr(config, "CALIBRATION_JOURNAL_PATH", str(ledger_root / "journal.json"))
    monkeypatch.setattr(config, "CALIBRATION_SAMPLES_PATH", str(ledger_root / "calibration_samples.json"))
    yield
