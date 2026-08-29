"""统计校准层测试（v6.3 S2，校准层 + 样本门槛）。

覆盖：
  ① n<50 降级为区间表述（Wilson 区间）+ 固定披露"校准样本积累中（n/50）"；
  ② n≥50 输出校准曲线点与单调性检测；
  ③ 隔离性：不读未结算记录（open/void 一律不进样本库）、不触碰决策输入；
  ④ 桶统计数值正确性（构造已知样本，胜率/期望R/Wilson 区间逐一核对）。
"""

from __future__ import annotations

import json

import pytest

from trading_system import config
from trading_system.calibration import CalibrationLayer, wilson_interval


def _rec(ticker, tss, r, status="closed", date="2026-08-01"):
    return {"date": date, "ticker": ticker, "tss_final": tss, "mrs_star": 7.0,
            "r": r, "win": (r is not None and r > 0), "status": status}


def _layer(tmp_path, min_samples=None):
    return CalibrationLayer(
        journal_path=str(tmp_path / "journal.json"),
        samples_path=str(tmp_path / "calibration_samples.json"),
        min_samples=min_samples)


# ---------------------------------------------------------------- ① n<50 降级
def test_below_threshold_interval_only(tmp_path):
    layer = _layer(tmp_path)
    samples = [_rec(f"T{i}", 8.0, 1.0 if i % 2 else -1.0) for i in range(30)]
    out = layer.summarize(samples)
    assert out["status"] == "accumulating"
    assert out["n"] == 30 and out["curve"] is None and out["monotonic"] is None
    assert f"校准样本积累中（30/{config.CALIBRATION_MIN_SAMPLES}）" in out["disclosure"]
    # 区间表述：每桶都有 Wilson 区间，不输出点估计结论
    for b in out["buckets"]:
        assert "wilson" in b and len(b["wilson"]) == 2


# ---------------------------------------------------------------- ② n≥50 校准曲线
def test_above_threshold_curve_and_monotonicity(tmp_path):
    layer = _layer(tmp_path)
    # 构造单调样本：低桶全亏、中桶胜负各半、高桶全胜
    samples = ([_rec(f"L{i}", 7.5, -1.0) for i in range(20)]
               + [_rec(f"M{i}", 8.0, 1.0 if i % 2 else -1.0) for i in range(20)]
               + [_rec(f"H{i}", 9.0, 1.5) for i in range(20)])
    out = layer.summarize(samples)
    assert out["status"] == "calibrated" and out["n"] == 60
    assert out["curve"] and len(out["curve"]) == 3
    assert out["monotonic"] is True                       # 0 → 0.5 → 1.0 单调
    rates = [c["win_rate"] for c in out["curve"]]
    assert rates == [0.0, 0.5, 1.0]


def test_non_monotonic_detected(tmp_path):
    layer = _layer(tmp_path)
    samples = ([_rec(f"L{i}", 7.5, 1.0) for i in range(25)]
               + [_rec(f"H{i}", 9.0, -1.0) for i in range(25)])
    out = layer.summarize(samples)
    assert out["status"] == "calibrated" and out["monotonic"] is False
    assert "不成立" in out["disclosure"]


# ---------------------------------------------------------------- ③ 隔离性
def test_only_settled_records_enter_samples(tmp_path):
    layer = _layer(tmp_path)
    records = [
        _rec("WIN", 8.0, 1.2),                            # closed ✓
        _rec("OPEN", 8.5, None, status="open"),           # 未结算 ✗
        _rec("VOID", 8.3, None, status="void"),           # 作废 ✗
        {**_rec("NOR", 8.1, None)},                       # closed 但 r 缺失 ✗
    ]
    samples = layer.collect_samples(records)
    assert [s["ticker"] for s in samples] == ["WIN"]      # 只读已结算


def test_run_reads_journal_and_saves_samples(tmp_path):
    (tmp_path / "journal.json").write_text(json.dumps(
        [_rec("A", 8.0, 1.0), _rec("B", 7.6, -1.0, status="open")]))
    layer = _layer(tmp_path)
    out = layer.run()
    assert out["n"] == 1
    saved = json.loads((tmp_path / "calibration_samples.json").read_text())
    assert len(saved) == 1 and saved[0]["ticker"] == "A"


def test_isolation_no_decision_path_import():
    """静态审查：校准层不得 import 决策链路（闸门/风控/回测），
    源码不得出现分数写入——它是会计账，不是决策输入。"""
    import inspect

    from trading_system import calibration
    src = inspect.getsource(calibration)
    for forbidden in ["pass_gates", "risk_manager", "backtest", "gate import",
                      "tss_final =", "mrs_star =", "position_cap"]:
        assert forbidden not in src, f"校准层疑似决策输入路径: {forbidden}"


def test_pipeline_gate_output_immune_to_calibration(tmp_path, monkeypatch):
    """端到端隔离：校准样本存在与否，闸门放行结果逐位一致。"""
    from trading_system.pipeline import run_pipeline

    monkeypatch.setattr(config, "CALIBRATION_JOURNAL_PATH",
                        str(tmp_path / "journal.json"))
    monkeypatch.setattr(config, "CALIBRATION_SAMPLES_PATH",
                        str(tmp_path / "samples.json"))
    monkeypatch.setattr(config, "DEBATE_ENABLED", False)

    def snapshot(r):
        return (r.action, r.mrs.mrs_star,
                [(p.ticker, p.tss_final, p.tos, p.shares, p.card) for p in r.picks])

    r1 = run_pipeline(provider_name="demo", universe_mode="core",
                      top_n=8, max_picks=3)
    # 写入一批校准样本后再跑——决策输出必须不变（样本只进报告层）
    (tmp_path / "journal.json").write_text(json.dumps(
        [_rec(f"T{i}", 8.0, 1.0) for i in range(60)]))
    r2 = run_pipeline(provider_name="demo", universe_mode="core",
                      top_n=8, max_picks=3)
    assert snapshot(r1) == snapshot(r2)
    assert r2.raw["calibration"]["status"] == "calibrated"   # 报告层照常披露
    steps = {s["step"] for s in r2.raw["redline"]}
    assert "iteration.calibration" in steps                  # 每日被点名


# ---------------------------------------------------------------- ④ 桶统计数值正确性
def test_bucket_statistics_exact(tmp_path):
    layer = _layer(tmp_path)
    samples = [
        _rec("A", 7.5, 1.0), _rec("B", 7.6, -1.0),        # 桶1: 1/2 胜, 均R 0
        _rec("C", 8.0, 2.0), _rec("D", 8.4, 1.0),
        _rec("E", 8.2, -1.0), _rec("F", 7.9, 0.5),        # 桶2(7.8-8.5): 3/4 胜, 均R 0.625
        _rec("G", 9.0, 3.0),                              # 桶3: 1/1 胜
        _rec("H", 7.0, 5.0),                              # 低于首桶下限 → 不计入任何桶
    ]
    out = layer.summarize(samples)
    b1, b2, b3 = out["buckets"]
    assert (b1["n"], b1["wins"], b1["win_rate"], b1["avg_r"]) == (2, 1, 0.5, 0.0)
    assert (b2["n"], b2["wins"], b2["win_rate"], b2["avg_r"]) == (4, 3, 0.75, 0.625)
    assert (b3["n"], b3["wins"], b3["win_rate"], b3["avg_r"]) == (1, 1, 1.0, 3.0)


def test_wilson_interval_known_values():
    # n=4 全胜：Wilson 95% ≈ [0.5101, 1.0]（手算核对值）
    lo, hi = wilson_interval(4, 4)
    assert lo == pytest.approx(0.5101, abs=1e-3) and hi == 1.0
    # 区间恒包含点估计；空样本给全区间
    lo, hi = wilson_interval(3, 4)
    assert lo <= 0.75 <= hi
    assert wilson_interval(0, 0) == (0.0, 1.0)
