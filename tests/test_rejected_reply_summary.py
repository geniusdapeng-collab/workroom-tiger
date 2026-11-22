"""A rejected paid reply belongs to native accounting, not accepted coverage."""

from test_research_work_proof import (
    observed,
    schema_retry_exhausted,
    validation_context,
)
from trading_system.llm.work_proof import ResearchWorkProof


def test_rejected_paid_replies_do_not_inflate_accepted_model_summary(schema_retry_exhausted):
    fixture = schema_retry_exhausted
    verifier = ResearchWorkProof(fixture.replay)
    index = 0
    for original in fixture.originals:
        _, consumed = verifier.verify_document(
            original, fixture.joins[index:],
            validation_context=validation_context(fixture), return_count=True)
        index += consumed
    assert index == len(fixture.joins)

    accounting = verifier.assert_complete()
    observations = fixture.replay.chat_observations
    accepted = [row["response_id"] for row in observations if row["diagnostic"] is None]
    rejected = [row["response_id"] for row in observations if row["diagnostic"] is not None]
    assert len(accepted) == len(rejected) == 2
    assert accounting["native_count"] == len(observations) == len(fixture.wires) == 4
    assert accounting["known_usage"] == fixture.store.status()["known_usage"]
    assert accounting["format_reanswers"] == 1
    assert all(row["usage"] is not None for row in observations)
    assert set(rejected).isdisjoint(accepted)
    assert fixture.replay.response_ids == accepted
    assert fixture.replay.counts["clean.llm_semantic"] == len(accepted)
