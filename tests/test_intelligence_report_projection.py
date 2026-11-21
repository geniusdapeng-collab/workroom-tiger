from copy import deepcopy
from dataclasses import dataclass
import math

from trading_system.agent_evidence import normalize
from trading_system.llm import intelligence_proof
from trading_system.report import project_report


@dataclass(frozen=True)
class Candidate:
    detail: dict


def test_report_proof_matches_actual_serializer_without_changing_frozen_stage():
    candidate = Candidate({"finite": 3.25, "zero": 0, "flag": False, "empty": None,
                           "missing": [float("nan"), float("inf"), -float("inf")]})
    report = {"watchlist": [candidate]}
    rendered, disclosed = project_report(report)
    frozen = normalize(report)
    before = deepcopy(frozen)
    intelligence_proof._same(rendered, intelligence_proof._report_projection(frozen), "report.emit")
    assert frozen == before and math.isnan(candidate.detail["missing"][0])
    assert [row["reason"] for row in disclosed] == ["nan", "positive_infinity", "negative_infinity"]
    assert rendered["watchlist"][0]["detail"] == {"finite": 3.25, "zero": 0, "flag": False,
                                                  "empty": None, "missing": [None, None, None]}


def test_report_proof_only_projects_known_exact_missing_markers():
    markers = [
        {"__type__": "missing", "reason": "pandas_na"},
        {"__type__": "missing", "reason": "unknown"},
        {"__type__": "other", "reason": "nan"},
        {"__type__": "missing", "reason": "nan", "extra": True},
    ]
    assert intelligence_proof._report_projection({"observed": markers}) == {"observed": [None, *markers[1:]]}
