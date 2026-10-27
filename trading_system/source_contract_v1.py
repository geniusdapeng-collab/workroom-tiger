"""Trusted complete executable-path contract for strict clean output v1.

This reviewed release registry is a literal, independent of a run's manifest
and frozen directory. Source bytes still come from the admission hash-bound
manifest; this registry proves the whole release path closure, not a remote
signature or protection against someone rewriting the trusted verifier itself.

Any change to the executable closure must deliberately update/version this
registry together with its canonical-inventory regression. Optional calendar
content is independently bound by the existing calendar admission contract.
"""
from __future__ import annotations

STRICT_SOURCE_CONTRACT_V1 = "tiger.strict-source-inventory.v1"
REQUIRED_SOURCE_PATHS = frozenset((
    "bundles/trading/agent-workflow.schema.json",
    "bundles/trading/capabilities/required-v2-contract.mjs",
    "bundles/trading/capabilities/workflow-client.mjs",
    "main.py",
    "requirements.txt",
    "scripts/tiger-agent-config.mjs",
    "scripts/tiger-agent-mcp.mjs",
    "scripts/tiger-agent.mjs",
    "trading_system/__init__.py",
    "trading_system/activity.py",
    "trading_system/agent_evidence.py",
    "trading_system/agent_runner.py",
    "trading_system/agent_runtime.py",
    "trading_system/agent_supervisor.py",
    "trading_system/agents/__init__.py",
    "trading_system/agents/base.py",
    "trading_system/agents/chain_cycle_agent.py",
    "trading_system/agents/debate_agent.py",
    "trading_system/agents/mrs_agent.py",
    "trading_system/agents/narrative_agent.py",
    "trading_system/agents/risk_manager_agent.py",
    "trading_system/agents/sector_agent.py",
    "trading_system/agents/tss_agent.py",
    "trading_system/agents/universe_scanner_agent.py",
    "trading_system/backtest.py",
    "trading_system/business_evaluation.py",
    "trading_system/calendar.py",
    "trading_system/calibration.py",
    "trading_system/chains.py",
    "trading_system/cleaning/__init__.py",
    "trading_system/cleaning/pipeline.py",
    "trading_system/config.py",
    "trading_system/currency.py",
    "trading_system/data_models.py",
    "trading_system/events.py",
    "trading_system/execution_policy.py",
    "trading_system/exit_engine.py",
    "trading_system/gate.py",
    "trading_system/governance_bridge.py",
    "trading_system/hero_art.py",
    "trading_system/indicators.py",
    "trading_system/journal.py",
    "trading_system/ledger_io.py",
    "trading_system/llm/__init__.py",
    "trading_system/llm/candidate_research.py",
    "trading_system/llm/clean_output.py",
    "trading_system/llm/client.py",
    "trading_system/llm/deepseek_required.py",
    "trading_system/llm/market_evidence.py",
    "trading_system/llm/prompts.py",
    "trading_system/llm/request_budget.py",
    "trading_system/llm/required.py",
    "trading_system/llm/required_proof.py",
    "trading_system/llm/required_proof_v2.py",
    "trading_system/llm/required_schemas.py",
    "trading_system/llm/scene_policy.py",
    "trading_system/markets/__init__.py",
    "trading_system/markets/base.py",
    "trading_system/markets/cn.py",
    "trading_system/markets/hk.py",
    "trading_system/markets/registry.py",
    "trading_system/markets/us.py",
    "trading_system/opportunity_lifecycle.py",
    "trading_system/options_metrics.py",
    "trading_system/paper_policy.py",
    "trading_system/pipeline.py",
    "trading_system/portfolio/__init__.py",
    "trading_system/portfolio/allocator.py",
    "trading_system/portfolio/sentinel.py",
    "trading_system/portfolio/steward.py",
    "trading_system/providers/__init__.py",
    "trading_system/providers/agentgw.py",
    "trading_system/providers/base.py",
    "trading_system/providers/cn_native.py",
    "trading_system/providers/demo.py",
    "trading_system/providers/eastmoney.py",
    "trading_system/providers/ifind_gw.py",
    "trading_system/providers/official.py",
    "trading_system/providers/sina.py",
    "trading_system/providers/stooq.py",
    "trading_system/providers/tencent.py",
    "trading_system/providers/tiingo.py",
    "trading_system/providers/yahoo.py",
    "trading_system/publication_projection_v2.py",
    "trading_system/redline/__init__.py",
    "trading_system/redline/execution.py",
    "trading_system/redline/model_diagnostics.py",
    "trading_system/report.py",
    "trading_system/report_assets.py",
    "trading_system/report_html.py",
    "trading_system/review/__init__.py",
    "trading_system/review/attribution.py",
    "trading_system/review/chief.py",
    "trading_system/review/monthly.py",
    "trading_system/review/weekly.py",
    "trading_system/search/__init__.py",
    "trading_system/search/credibility.py",
    "trading_system/search/hub.py",
    "trading_system/search/models.py",
    "trading_system/search/sources.py",
    "trading_system/sector_classification.py",
    "trading_system/simulator.py",
    "trading_system/source_contract_v1.py",
    "trading_system/state.py",
    "trading_system/stats.py",
    "trading_system/tech_chain/__init__.py",
    "trading_system/tech_chain/agents.py",
    "trading_system/tech_chain/fusion.py",
    "trading_system/tech_chain/universe.py",
    "trading_system/triggers.py",
    "trading_system/universe.py",
    "trading_system/workflow_runtime_v2.py",
    "trading_system/workflow_v2.py",
))
OPTIONAL_SOURCE_PATHS = frozenset({"events_calendar.json"})


def validate_source_paths(paths: object, *, contract_id: object) -> None:
    """Require the exact registered release closure plus explicit optionals."""
    if (type(contract_id) is not str or contract_id != STRICT_SOURCE_CONTRACT_V1
            or type(paths) is not list or any(type(path) is not str for path in paths)
            or len(paths) != len(set(paths))):
        raise ValueError("Invalid strict source inventory contract")
    observed = frozenset(paths)
    if (not REQUIRED_SOURCE_PATHS.issubset(observed)
            or observed - REQUIRED_SOURCE_PATHS - OPTIONAL_SOURCE_PATHS):
        raise ValueError("Strict source inventory differs from registered release")
