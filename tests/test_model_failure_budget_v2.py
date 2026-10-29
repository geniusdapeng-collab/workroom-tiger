"""Failed paid calls keep bounded accounting without storing rejected text."""
import json

import pytest

import main
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.redline.execution import RequiredModelFailed
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from test_short_term_core_v2 import upgrade_fixture


@pytest.mark.parametrize("failure", ["incomplete", "timeout", "usage_over_bound"])
def test_real_orchestration_persists_failed_call_budget_and_never_commits_account(
        tmp_path, monkeypatch, failure):
    execution, root, args, _, denied, attempts = upgrade_fixture(tmp_path, monkeypatch)
    before = execution.paths.sim_path.read_bytes()
    original = DeepSeekRequiredClient._post_json
    paid = []

    def post(client, path, request):
        if path != "/chat/completions":
            return original(client, path, request)
        paid.append(path)
        if failure == "timeout":
            raise RequiredModelUnavailable("NETWORK_TIMEOUT")
        response, body_hash = original(client, path, request)
        if failure == "incomplete":
            response["choices"][0]["finish_reason"] = "length"
            response["choices"][0]["message"]["content"] = "PRIVATE_REJECTED_MODEL_TEXT"
        else:
            response["usage"] = {"prompt_tokens": 100, "completion_tokens": 999999,
                                 "total_tokens": 1000099}
        return response, body_hash

    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
    with pytest.raises(RequiredModelFailed):
        main._daily(args, "yahoo", execution=execution)
    errors = []
    for file in (root / "evidence").glob("*.json"):
        value = json.loads(file.read_text()).get("value")
        if isinstance(value, dict) and value.get("error_type") == "RequiredModelUnavailable":
            errors.append(value)
    assert len(errors) == 1 and len(paid) == 1
    error = errors[0]
    accounting = error["accounting"]
    assert accounting["schema"] == "tiger.model-call-accounting.v2"
    assert accounting["kind"] == "chat"
    assert accounting["counts"] == {"chat": 1, "search": 14, "fetch": 0}
    assert len(accounting["call_id"]) == 32 and len(accounting["request_sha256"]) == 64
    if failure == "incomplete":
        assert error["diagnostic"]["code"] == "INCOMPLETE_MODEL_RESPONSE"
        assert accounting["status"] == "settled"
        assert accounting["usage"] == {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}
        assert accounting["actual_tokens"] == 200 and accounting["reserved_tokens"] == 0
        assert accounting["frozen"] is False
        assert accounting["finish_reason"] == "length"
    else:
        assert accounting["status"] == "unknown" and accounting["usage"] is None
        assert accounting["frozen"] is True and accounting["reserved_tokens"] > 0
        assert accounting["actual_tokens"] == 0
        assert accounting["finish_reason"] == (None if failure == "timeout" else "stop")
    assert "PRIVATE_REJECTED_MODEL_TEXT" not in json.dumps(errors)
    assert execution.paths.sim_path.read_bytes() == before
    assert not (root / "receipt.json").exists() and denied == []


def test_accounting_boundary_rejects_unknown_text_and_exception_subclasses():
    from trading_system.redline.model_diagnostics import (
        model_failure_accounting, validate_model_failure_accounting,
    )
    with pytest.raises(ValueError) as caught:
        validate_model_failure_accounting({"content": "PRIVATE_REJECTED_MODEL_TEXT"})
    assert "PRIVATE_REJECTED_MODEL_TEXT" not in str(caught.value)

    class Untrusted(RequiredModelUnavailable):
        @property
        def accounting(self):
            raise AssertionError("Untrusted exception property must not be read")

    assert model_failure_accounting(Untrusted("NETWORK_TIMEOUT")) is None
    error = RequiredModelUnavailable("NETWORK_TIMEOUT")
    error._accounting = {"content": "PRIVATE_REJECTED_MODEL_TEXT"}
    assert model_failure_accounting(error) is None


@pytest.mark.parametrize("field", ["schema", "kind", "status", "finish_reason", "call_id", "request_sha256"])
def test_arbitrary_string_subclasses_cannot_cross_the_failure_accounting_boundary(field):
    from trading_system.redline.model_diagnostics import model_failure_accounting

    class Spoof(str):
        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

        __hash__ = str.__hash__

    error = RequiredModelUnavailable("NETWORK_TIMEOUT")
    error._accounting = {"schema": "tiger.model-call-accounting.v2", "kind": "chat",
        "call_id": "a" * 32, "request_sha256": "b" * 64, "status": "unknown", "usage": None,
        "counts": {"chat": 1, "search": 0, "fetch": 0}, "actual_tokens": 0,
        "reserved_tokens": 1000, "estimated_charged_usd": 0., "estimated_reserved_usd": .0012,
        "frozen": True, "finish_reason": "unknown"}
    error._accounting[field] = Spoof("PRIVATE_REJECTED_MODEL_TEXT")
    assert model_failure_accounting(error) is None
