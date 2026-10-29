"""Real synthetic 43-stage BUY publication must omit private account replay history."""
from datetime import datetime
import json
from pathlib import Path

from trading_system.agent_evidence import file_ref
from trading_system.agent_runtime import SOURCE_ROOT, business_summary, frozen_clock
from trading_system.ledger_io import read_json_strict, write_json_atomic
from test_root_integration_review_v2 import legal_buy_fixture


def test_complete_buy_summary_keeps_pending_plan_without_private_frozen_account(tmp_path, monkeypatch):
    import main
    execution, root, args, denied, _ = legal_buy_fixture(tmp_path, monkeypatch)
    account = read_json_strict(execution.paths.sim_path)
    sentinel = "PRIVATE_FROZEN_ACCOUNT_HISTORY_TEST_ONLY"
    account["ops_log"].append({"date": "2026-10-16", "equity": 100000., "private_audit_note": sentinel})
    write_json_atomic(execution.paths.sim_path, account)
    output = main._daily(args, "yahoo", execution=execution)
    assert output.sim_state["pending"]
    assert sentinel in json.dumps(output.result.raw["cohort_inputs"])
    assert sentinel in json.dumps(output.sim_state["ops_log"])
    for identity in output.result.raw["policy_identity"]["files"]:
        relative = Path(identity["path"])
        target = root / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SOURCE_ROOT / relative).read_bytes())
    write_json_atomic(root / "request.json", {"profile": "us-public-model-required-intraday-paper-v2"})
    admission = {"clock": frozen_clock(datetime.fromisoformat(execution.admitted_at_utc))}
    media = {".json": "application/json", ".jsonl": "application/x-ndjson",
             ".md": "text/markdown", ".html": "text/html"}
    artifacts = [file_ref(root, file.relative_to(root).as_posix(), media[file.suffix])
                 for file in sorted(execution.paths.reports_dir.rglob("*"))
                 if file.is_file() and not file.name.endswith(".lock")]
    summary = business_summary(root, admission, artifacts)
    assert [row["ticker"] for row in summary["pending"]] == ["NVDA"]
    assert summary["pending"][0]["plan_id"] == output.sim_state["pending"][0]["plan_id"]
    assert "entry_budget" not in summary["pending"][0]
    assert sentinel not in json.dumps(summary, ensure_ascii=False)
    assert denied == []
