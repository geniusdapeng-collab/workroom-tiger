"""围栏生成器测试（S5 任务二）。

① 生成确定性：同 config 同 YAML（规则语义逐条一致）；
② config 改动 → --check 报漂移；
③ 15 条基线规则全覆盖且 level=block，且每条带 config 来源注释（无手写阈值）。
"""

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_gen():
    spec = importlib.util.spec_from_file_location(
        "gen_fences", ROOT / "scripts/gen_fences.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generation_deterministic():
    """同 config → 同规则语义（生成时间戳仅在头部注释，不参与语义比对）。"""
    gen = _load_gen()
    rules1 = gen.build_rules()
    rules2 = gen.build_rules()
    assert gen.render_yaml(rules1, "H", "T") == gen.render_yaml(rules2, "H", "T")
    assert gen.config_hash(rules1) == gen.config_hash(rules2)
    sem1 = gen._semantic(gen.render_yaml(rules1, "H1", "T1"))
    sem2 = gen._semantic(gen.render_yaml(rules2, "H2", "T2"))
    assert sem1 == sem2


def test_baseline_rules_full_coverage_block():
    """15 条基线规则全覆盖、全部 level=block、每条带 source 注释。"""
    gen = _load_gen()
    rules = gen.build_rules()
    baseline = [r for r in rules if r["is_baseline"]]
    assert len(baseline) == 15
    assert [r["rule_id"] for r in baseline] == [f"R-T{i}" for i in range(1, 16)]
    assert all(r["level"] == "block" for r in baseline)
    text = gen.render_yaml(rules, "H", "T")
    # 每条规则上方必须带 "# source:" 注释（config→fence 映射留痕，无手写阈值）
    lines = text.splitlines()
    rule_lines = [i for i, ln in enumerate(lines) if ln.startswith("  - rule_id:")]
    assert len(rule_lines) == len(rules)
    for i in rule_lines:
        assert lines[i - 1].startswith("  # source:"), f"缺 source 注释: {lines[i]}"
    # 关键阈值必须来自 config 插值（出现在 when 表达式中）
    from trading_system import config
    assert f"params.risk_pct > {config.RISK_R_PCT!r}" in text or "0.008" in text
    assert "params.mrs < 4.0" in text            # MRS_GATE_BLOCK
    assert "params.mrs < 6.0" in text            # MRS_GATE_LIGHT
    assert "after.position_pct > 0.2" in text    # MAX_SINGLE_POSITION_PCT
    assert "params.profit_r >= 2.0" in text      # PROFIT_PROTECT_R
    assert "params.size_ratio < 0.3" in text     # LIGHT_PROBE.size_ratio
    doc = yaml.safe_load(text)
    assert doc["default_level"] == "review"
    assert doc["version"] == gen.FENCE_VERSION


def test_check_detects_drift_on_config_change(monkeypatch, tmp_path):
    """config 阈值改动 → --check 检出漂移（退出码 1）。"""
    gen = _load_gen()
    from trading_system import config
    # 先用当前 config 生成一份 YAML
    rules = gen.build_rules()
    text = gen.render_yaml(rules, "H", "T")
    fence = tmp_path / "trading-baseline.yml"
    fence.write_text(text, encoding="utf-8")
    monkeypatch.setattr(gen, "FENCE_PATH", fence)
    assert gen.check() == 0          # 无漂移
    # 改 config 阈值（单票上限 20%→15%）→ 必须报漂移
    monkeypatch.setattr(config, "MAX_SINGLE_POSITION_PCT", 0.15)
    assert gen.check() == 1
