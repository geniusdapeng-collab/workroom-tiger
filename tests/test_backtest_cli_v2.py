"""The public CLI must select the same execution version as its printed result."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("selected", [None, "whitepaper-execution-v2", "legacy-execution-v1"])
def test_backtest_cli_selects_execution_version_current_nav_and_position_limit(tmp_path, monkeypatch, capsys, selected):
    import main
    from trading_system import backtest, providers, universe
    expected = selected or "whitepaper-execution-v2"
    seen = []
    monkeypatch.setattr(providers, "get_provider", lambda *_: SimpleNamespace(name="SYNTHETIC_TEST"))
    monkeypatch.setattr(universe, "load_universe", lambda *_: ["AAA"])
    monkeypatch.setattr(backtest, "collect_day_frames", lambda *args, **kwargs: ([], None, {}))
    def run(frames, panel, params):
        seen.append(params)
        return {"execution_policy": params.execution_policy,
                "evidence_scope": "SYNTHETIC_TEST numeric replay, never required-model alpha",
                "limitations": ["SYNTHETIC_TEST no market history"], "n_days": 0,
                "n_trades": 0, "win_rate": 0., "expectancy_r": 0., "profit_factor": None,
                "total_r": 0., "port_total_return": 0., "port_sharpe": 0.,
                "port_max_dd": 0., "by_template": {}}
    monkeypatch.setattr(backtest, "run_backtest", run)
    args = SimpleNamespace(demo=False, universe="core", universe_file=None, bt_days=20,
        top=4, account=50000., picks=9, out=str(tmp_path))
    if selected:
        args.bt_execution_policy = selected
    main._backtest(args, "yahoo")
    assert seen[0].execution_policy == expected
    assert seen[0].initial_cash == 50000. and seen[0].max_positions == 9
    text = capsys.readouterr().out
    assert expected in text
    assert "numeric replay" in text and "样本不足" in text
    assert (tmp_path / "回测_SYNTHETIC_TEST.md").read_text().strip() in text


def test_cli_rejects_an_unknown_backtest_execution_version_before_work(monkeypatch):
    import main
    monkeypatch.setattr("sys.argv", ["main.py", "--backtest", "--bt-execution-policy", "unsupported"])
    monkeypatch.setattr(main, "_backtest", lambda *_: pytest.fail("Invalid version must fail at the CLI boundary"))
    with pytest.raises(SystemExit) as stopped:
        main.main()
    assert stopped.value.code == 2
