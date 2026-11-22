"""Production document worker and proof for a schema-rejected summary response."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
import requests

from test_deepseek_json_observation import native
from test_deepseek_required import Response
from test_research_work_proof import (
    FixtureReplay,
    observed,
    prepare_proof_context,
    raw,
    segmented_transport,
    validation_context,
)
from trading_system.llm.work_proof import ResearchWorkProof
from trading_system.llm.work_required import clean_round
from trading_system.research_work import _POLICY


def test_rejected_metadata_summary_seals_terminal_and_replays_without_network(observed, monkeypatch):
    monkeypatch.setitem(_POLICY, "segment_input_token_limit", 5000)
    monkeypatch.setitem(_POLICY, "document_workers", 1)
    admission, policy, acquisition, client = prepare_proof_context(observed)
    wires = segmented_transport(monkeypatch)
    accept = requests.post

    def reject_summary(url, **kwargs):
        response = accept(url, **kwargs)
        if '"phase":"metadata_synthesis"' not in wires[-1]["messages"][-1]["content"]:
            return response
        response = Response(native('{"summary_zh":[]}', "summary-rejected-" + str(len(wires))))
        response.url, response.history = url, []
        return response

    monkeypatch.setattr(requests, "post", reject_summary)
    content = "\n\n".join(
        f"Section {number}. Complete issuer disclosure paragraph with revenue "
        f"and risk evidence number {number}." for number in range(1, 401))
    rejected = raw(content=content)
    sibling = raw("source-2")
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "schema-rejected metadata synthesis"}) as scope:
        documents = clean_round(client, [rejected, sibling], acquisition=acquisition)
        scope.output(documents)
        joins = deepcopy(scope.io_calls)
    assert [document.raw.doc_id for document in documents] == [sibling.doc_id]

    replay = FixtureReplay(observed, admission, policy)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("Proof opened a network connection"))
    verifier = ResearchWorkProof(replay)
    context = validation_context(SimpleNamespace(acquisition=acquisition))
    document, consumed = verifier.verify_document(
        rejected, joins, validation_context=context, return_count=True)
    assert document is None
    good_document, good_count = verifier.verify_document(
        sibling, joins[consumed:], validation_context=context, return_count=True)
    assert good_document.raw.doc_id == sibling.doc_id
    assert consumed + good_count == len(joins)

    summary = verifier.store.read_result(f"{rejected.doc_id}:segment-summary")["value"]
    assert summary["child_seal_ref"] and summary["output"]["phase"] == "metadata_synthesis"
    assert summary["output"]["error_code"] == "WORK_OUTPUT_SCHEMA_MISMATCH"
    dispositions = verifier.store.document_dispositions()["documents"]
    assert dispositions[rejected.doc_id]["state"] == "unverifiable"
    assert dispositions[sibling.doc_id]["state"] == "eligible"
    accounting = verifier.assert_complete()
    assert accounting["native_count"] == len(wires)
    rejected_observations = [row for row in replay.chat_observations if row["diagnostic"] is not None]
    assert len(rejected_observations) == 2
    assert len(replay.response_ids) + len(rejected_observations) == len(wires)
    assert all(row["response_id"] not in replay.response_ids and row["usage"] is not None
               for row in rejected_observations)
