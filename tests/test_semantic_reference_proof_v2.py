"""Full synthetic workflow: downstream fact references bind completed cleaning.

These cases never call a model/provider. They change a model request, recompute
its file/event/request hashes, and require the semantic reconstruction itself
to reject it before budget settlement can supply an unrelated failure.
"""
from copy import deepcopy
import hashlib
import importlib
import json

import pytest

from trading_system.agent_evidence import (
    EvidenceInvalid, SNAPSHOT_SCHEMA, atomic_json, file_ref, read_json,
    read_snapshot, verify_events,
)
from trading_system.redline.execution import daily_stage_contract
from test_required_proof_v2 import complete_fixture, h, rewrite_stage


@pytest.mark.parametrize("tamper", ["quote_hash", "quote_length", "source_identity", "statement"])
def test_rehashed_semantic_reference_cannot_replace_clean_source_fact(
        tmp_path, monkeypatch, tamper):
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    execution, root, result, admission, denied, _ = complete_fixture(tmp_path, monkeypatch)
    assert module.verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    clean_end = next(row for row in execution.summary() if row["stage"] == "clean.cross_validate")
    clean = read_snapshot(root, clean_end["output_ref"], run_id=root.name,
                          stage="clean.cross_validate", role="output")["value"]
    quotes_by_source = {row["fields"]["raw"]["fields"]["doc_id"]: {
        hashlib.sha256(claim["quote"].encode()).hexdigest(): len(claim["quote"])
        for claim in row["fields"]["claims"]}
        for row in clean["scoring_documents"]}
    original_quotes = deepcopy(quotes_by_source)

    def change(_, output):
        call = output["io_calls"][0]
        request_blob = read_json(root, call["input_ref"]["path"])
        request = request_blob["value"]
        framed = request["request"]["messages"][1]["content"]
        task, remainder = framed.split("\n", 1)
        raw_payload, schema_framing = remainder.split("\nJSON schema:\n", 1)
        payload = json.loads(raw_payload)
        table = payload["document_index"]
        assert isinstance(table, dict) and set(table) == {"columns", "rows"}
        assert set(payload["document_delivery"]) == {
            "body_delivery", "quote_delivery", "quote_source_scope", "table_encoding"}
        positions = {name: index for index, name in enumerate(table["columns"])}
        row = next(row for row in table["rows"] if row[positions["claims"]]["rows"])
        source_id = row[positions["id"]]
        claims = row[positions["claims"]]
        claim_positions = {name: index for index, name in enumerate(claims["columns"])}
        claim = claims["rows"][0]
        quote_ref = claim[claim_positions["quote_ref"]]
        assert set(quote_ref) == {"sha256", "characters"}
        assert quotes_by_source[source_id][quote_ref["sha256"]] == quote_ref["characters"]
        if tamper == "quote_hash":
            quote_ref["sha256"] = "f" * 64 if quote_ref["sha256"] != "f" * 64 else "e" * 64
        elif tamper == "quote_length":
            quote_ref["characters"] += 1
        elif tamper == "source_identity":
            other = next(item for item in table["rows"]
                         if item[positions["id"]] != source_id)
            row[positions["id"]], other[positions["id"]] = (
                other[positions["id"]], source_id)
        else:
            claim[claim_positions["statement"]] += " SYNTHETIC_TEST invented interpretation"
        request["request"]["messages"][1]["content"] = (
            task + "\n" + json.dumps(payload, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False) + "\nJSON schema:\n" + schema_framing)
        request["request_sha256"] = h(request["request"])
        atomic_json(root, call["input_ref"]["path"], request_blob)
        call["input_ref"] = file_ref(root, call["input_ref"]["path"], SNAPSHOT_SCHEMA)
        proof_blob = read_json(root, call["output_ref"]["path"])
        proof_blob["value"]["request_sha256"] = request["request_sha256"]
        atomic_json(root, call["output_ref"]["path"], proof_blob)
        call["output_ref"] = file_ref(root, call["output_ref"]["path"], SNAPSHOT_SCHEMA)

    rewrite_stage(root, "tech.sentiment", change)
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    with pytest.raises(EvidenceInvalid) as caught:
        module.verify_required_model_v2(root, result, admission)
    assert caught.value.stage == "tech.sentiment"
    assert "request_identity_or_payload" in str(caught.value)
    assert quotes_by_source == original_quotes and denied == []
