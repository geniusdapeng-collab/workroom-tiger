"""Read-only, request-complete replay of the v2 required-model workflow.

The client below has no transport, environment lookup or network fallback. It
rebuilds production requests, consumes exactly the sealed native/model calls,
and runs the production semantic validators and projections again. This is a
cooperative local integrity proof, not an external signature or an alpha test.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
from types import SimpleNamespace

from ..agent_evidence import (EvidenceInvalid, normalize, read_bytes, read_snapshot,
                              read_json, safe_path, strict_json, verify_events, digest, json_bytes)
from ..redline import LLMUnavailable
from ..redline.execution import (ExecutionPolicy, ExecutionContractError, RequiredModelFailed, StrictObservationError, UPGRADE_MODEL_STAGES,
                                 daily_stage_contract)
from .deepseek_required import (BASE_URL, MODEL, RELEASE, SEARCH_MODE,
    DeepSeekRequiredClient, build_chat_request, sanitize_url, stage_reasoning_effort,
    stage_thinking_mode, validate_schema, responses_usage)
from .request_budget import BudgetGuard, BudgetRefused, LIMITS
from .clean_output import (CLEAN_OUTPUT_PATH, clean_output_policy,
                           build_clean_request, build_clean_safe_response,
                           CLEAN_OUTPUT_RESPONSES_POLICY_V1, CLEAN_OUTPUT_RESPONSES_PATH,
                           CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1, clean_slots_schema_v1,
                           build_clean_slots_responses_request, build_clean_responses_request,
                           build_clean_responses_safe_response)

_MODEL_METHODS = {"model.deepseek.search", "model.deepseek.fetch", "model.deepseek.chat"}
_COLLECTION_AND_MODEL = ("search.collect", *UPGRADE_MODEL_STAGES)


def _bad(reason="semantic_replay", *, stage=None):
    raise EvidenceInvalid("MODEL_REQUIRED_FAILED", "Required v2 evidence differs: " + reason, stage=stage)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _same(left, right):
    return _hash(normalize(left)) == _hash(normalize(right))


def _instant(value):
    if not isinstance(value, str):
        _bad("aware_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _bad("UTC_timestamp")
    return parsed


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value) is not None


class ReplayClient:
    """Closed per-stage I/O queues with a single reconstructed global budget."""
    required = True

    def __init__(self, execution, stage_calls):
        self.execution = execution
        self._calls = deepcopy(stage_calls)
        self._stage_name, self._cursor = None, 0
        self._visited, self._used_ids, self._response_ids, self._tool_call_ids = set(), set(), set(), set()
        self._output_message_ids = set()
        self._first_started, self._last_finished, self._clock = None, None, 0.
        self.receipts, self.search_receipts, self.fetch_receipts = [], [], []
        self.budget = BudgetGuard.for_policy(getattr(execution.policy, "budget_policy_id", None),
                                            clean_output_policy_id=clean_output_policy(execution),
                                            clock=lambda: self._clock)
        # Pure sanitization methods do not initialize the real client, consult
        # configuration or retain a key. Persisted text was already redacted.
        self._sanitizer = DeepSeekRequiredClient.__new__(DeepSeekRequiredClient)
        self._sanitizer._api_key = "__TIGER_NON_SECRET_OFFLINE_REPLAY_SENTINEL_7b88ac79__"
        _instant(execution.admitted_at_utc)

    def set_stage(self, stage):
        if self._stage_name is not None:
            self.finish_stage()
        if stage not in _COLLECTION_AND_MODEL or stage in self._visited or stage not in self._calls:
            _bad("stage_identity", stage=stage)
        self._stage_name, self._cursor = stage, 0
        self._visited.add(stage)

    def finish_stage(self):
        if self._stage_name is None or self._cursor != len(self._calls[self._stage_name]):
            _bad("unconsumed_stage_IO", stage=self._stage_name)
        self._stage_name = None

    def finish(self):
        if self._stage_name is not None:
            self.finish_stage()
        if self._visited != set(self._calls):
            _bad("unvisited_model_stage")

    def _consume(self, method, body, request_schema, proof_schema, request_extra, proof_extra,
                 *, endpoint_path=None):
        stage = self._stage_name
        if stage is None or self._cursor >= len(self._calls[stage]):
            _bad("missing_expected_IO", stage=stage)
        call = self._calls[stage][self._cursor]
        self._cursor += 1
        if not isinstance(call, dict) or set(call) != {"method", "request", "proof"} or call["method"] != method:
            _bad("IO_method_or_order", stage=stage)
        request, proof = call["request"], call["proof"]
        req_fields = {"schema", "call_id", "stage", "provider", "method", "endpoint", "request",
                      "request_sha256", "budget_reservation", "started_at_utc"} | request_extra
        proof_fields = {"schema", "call_id", "stage", "provider", "request_sha256",
                        "transport_response_sha256", "budget_reservation", "budget_after",
                        "started_at_utc", "finished_at_utc", "elapsed_ms"} | proof_extra
        if (not isinstance(request, dict) or set(request) != req_fields
                or not isinstance(proof, dict) or set(proof) != proof_fields):
            _bad("closed_IO_fields", stage=stage)
        call_id = request["call_id"]
        kind = method.rsplit(".", 1)[1]
        endpoint = BASE_URL + (endpoint_path if endpoint_path is not None else
                              "/chat/completions" if kind == "chat" else "/v1/tools/" + kind)
        if (not _hex(call_id, 32) or call_id in self._used_ids
                or request["schema"] != request_schema or proof["schema"] != proof_schema
                or request["call_id"] != proof["call_id"]
                or request["stage"] != stage or proof["stage"] != stage
                or request["provider"] != "deepseek" or proof["provider"] != "deepseek"
                or request["method"] != "POST" or request["endpoint"] != endpoint
                or not _same(request["request"], body) or request["request_sha256"] != _hash(body)
                or proof["request_sha256"] != request["request_sha256"]
                or not _hex(proof["transport_response_sha256"], 64)):
            _bad("request_identity_or_payload", stage=stage)
        started, finished = _instant(request["started_at_utc"]), _instant(proof["finished_at_utc"])
        elapsed = proof["elapsed_ms"]
        if (proof["started_at_utc"] != request["started_at_utc"] or started < _instant(self.execution.admitted_at_utc)
                or finished < started or self._last_finished is not None and started < self._last_finished
                or type(elapsed) not in (float, int) or elapsed < 0
                or elapsed != max(0., round((finished - started).total_seconds() * 1000, 3))):
            _bad("call_time_or_overlap", stage=stage)
        if self._first_started is None:
            self._first_started = started
        if (finished - self._first_started).total_seconds() > self.budget.limits["deadline_seconds"]:
            _bad("recorded_call_deadline", stage=stage)
        deadline = getattr(self.execution, "deadline_at_utc", None)
        if deadline is not None and finished >= _instant(deadline):
            _bad("admission_deadline", stage=stage)
        self._clock = (started - self._first_started).total_seconds()
        reservation = self.budget.reserve(kind, call_id, body)
        if not _same(request["budget_reservation"], reservation) or not _same(proof["budget_reservation"], reservation):
            _bad("budget_reservation", stage=stage)
        self._used_ids.add(call_id)
        self._last_finished = finished
        return request, proof

    def _settle(self, kind, request, proof, usage):
        self.budget.settle(request["call_id"], usage)
        if not _same(self.budget.snapshot(), proof["budget_after"]):
            _bad("budget_settlement", stage=self._stage_name)
        omitted = {"schema", "response", "parsed"} if kind == "chat" else {"schema", "results"} if kind == "search" else {"schema", "page"}
        receipt = {key: deepcopy(value) for key, value in proof.items() if key not in omitted}
        {"chat": self.receipts, "search": self.search_receipts, "fetch": self.fetch_receipts}[kind].append(receipt)

    def _document(self, document, kind, request, proof):
        required = {"id", "url", "title", "content", "published", "source", "fetched_at", "content_hash"}
        if (not isinstance(document, dict) or set(document) not in (required, required | {"retrieval"})
                or kind == "fetch" and "retrieval" in document):
            _bad("native_document_fields", stage=self._stage_name)
        identity = {key: document[key] for key in ("url", "title", "content", "published")}
        if (sanitize_url(document["url"]) != document["url"]
                or not isinstance(document["title"], str) or not document["title"] or len(document["title"]) > 500
                or self._sanitizer._source_title(document["title"]) != document["title"]
                or not isinstance(document["content"], str) or not document["content"].strip()
                or self._sanitizer._text(document["content"]) != document["content"]
                or document["id"] != "ds_" + _hash(identity)[:24]
                or document["source"] != "deepseek_" + kind
                or document["content_hash"] != hashlib.sha256(document["content"].encode()).hexdigest()
                or kind == "fetch" and document["published"] is not None):
            _bad("native_document_identity", stage=self._stage_name)
        fetched = _instant(document["fetched_at"])
        if not _instant(request["started_at_utc"]) <= fetched <= _instant(proof["finished_at_utc"]):
            _bad("native_document_fetch_time", stage=self._stage_name)
        if document["published"] is not None:
            value = document["published"]
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
            if parsed is None or parsed.tzinfo is None:
                _bad("native_publication_time", stage=self._stage_name)
        if "retrieval" in document:
            retrieval = document["retrieval"]
            if (not isinstance(retrieval, dict) or set(retrieval) - {"authority", "rerank_score", "site_name"}
                    or any(value is not None and type(value) not in (str, int, float) for value in retrieval.values())
                    or not _same(self._sanitizer._clean(retrieval), retrieval)):
                _bad("native_retrieval_metadata", stage=self._stage_name)

    def search(self, query, count=4, need_content=True):
        if (not isinstance(query, str) or not query.strip() or not 1 <= len(query) <= 128
                or type(count) is not int or not 1 <= count <= 50 or type(need_content) is not bool):
            _bad("search_request", stage=self._stage_name)
        body = {"query": self._sanitizer._text(query), "count": count, "need_content": need_content}
        request, proof = self._consume("model.deepseek.search", body,
            "tiger.deepseek-search-request.v1", "tiger.deepseek-search.v1",
            {"query_sha256", "search_mode"},
            {"method", "search_mode", "query_sha256", "result_count", "usable_result_count", "response_sha256", "results"})
        query_hash = hashlib.sha256(body["query"].encode()).hexdigest()
        if (request["query_sha256"] != query_hash or proof["query_sha256"] != query_hash
                or request["search_mode"] != SEARCH_MODE or proof["search_mode"] != SEARCH_MODE
                or proof["method"] != "POST" or not isinstance(proof["results"], list)
                or type(proof["result_count"]) is not int or type(proof["usable_result_count"]) is not int
                or not 0 <= proof["usable_result_count"] == len(proof["results"]) <= proof["result_count"] <= count
                or proof["response_sha256"] != _hash({"results": proof["results"]})):
            _bad("native_search_response", stage=self._stage_name)
        for document in proof["results"]:
            self._document(document, "search", request, proof)
        self._settle("search", request, proof, None)
        return deepcopy(proof["results"])

    def fetch(self, url):
        safe = sanitize_url(url)
        if safe is None:
            _bad("unsafe_fetch_url", stage=self._stage_name)
        request, proof = self._consume("model.deepseek.fetch", {"url": safe},
            "tiger.deepseek-fetch-request.v1", "tiger.deepseek-fetch.v1", {"search_mode"},
            {"method", "search_mode", "response_sha256", "page"})
        if (request["search_mode"] != SEARCH_MODE or proof["search_mode"] != SEARCH_MODE or proof["method"] != "POST"
                or proof["response_sha256"] != _hash({"page": proof["page"]}) or proof["page"].get("url") != safe):
            _bad("native_fetch_response", stage=self._stage_name)
        self._document(proof["page"], "fetch", request, proof)
        self._settle("fetch", request, proof, None)
        return deepcopy(proof["page"])

    def complete_json(self, *, system, user, schema_hint, schema, max_tokens=16384,
                      temperature=.2, reasoning_effort="high"):
        output_policy = clean_output_policy(self.execution) if self._stage_name == "clean.llm_semantic" else None
        strict_clean = output_policy is not None
        expected_effort = "none" if strict_clean else stage_reasoning_effort(self._stage_name, upgrade=True)
        if (type(reasoning_effort) is not str
                or reasoning_effort != expected_effort):
            _bad("stage_reasoning_effort", stage=self._stage_name)
        arguments = dict(system=self._sanitizer._text(system), user=self._sanitizer._text(user),
            schema_hint=self._sanitizer._clean(schema_hint), schema=schema, max_tokens=max_tokens,
            temperature=temperature)
        if output_policy in (CLEAN_OUTPUT_RESPONSES_POLICY_V1, CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1):
            return self._complete_clean_responses(arguments, schema, output_policy)
        body = (build_clean_request(**arguments) if strict_clean else
                build_chat_request(**arguments, reasoning_effort=reasoning_effort,
                    thinking_mode=stage_thinking_mode(self._stage_name, upgrade=True)))
        request, proof = self._consume("model.deepseek.chat", body,
            "tiger.deepseek-request.v2" if strict_clean else "tiger.deepseek-request.v1",
            "tiger.deepseek-call.v2" if strict_clean else "tiger.deepseek-call.v1",
            {"output_schema", "schema_sha256", "release"} | ({"output_policy_id"} if strict_clean else set()),
            {"requested_model", "response_id", "model", "release", "usage", "finish_reason", "response_sha256",
             "schema_sha256", "response", "system_fingerprint", "search_mode", "parsed"}
             | ({"output_policy_id", "output_tool_call_id"} if strict_clean else set()),
            endpoint_path=CLEAN_OUTPUT_PATH if strict_clean else None)
        if (request["release"] != RELEASE or proof["release"] != RELEASE
                or proof["model"] != MODEL or proof["requested_model"] != MODEL
                or proof["finish_reason"] != ("tool_calls" if strict_clean else "stop")
                or strict_clean and (request["output_policy_id"] != output_policy or proof["output_policy_id"] != output_policy)
                or proof["search_mode"] != SEARCH_MODE
                or not _same(request["output_schema"], schema)
                or request["schema_sha256"] != _hash(schema) or proof["schema_sha256"] != request["schema_sha256"]):
            _bad("chat_contract", stage=self._stage_name)
        validate_schema(proof["parsed"], schema)
        if not _same(self._sanitizer._clean(proof["parsed"]), proof["parsed"]):
            _bad("unclean_model_output", stage=self._stage_name)
        response_id = DeepSeekRequiredClient._identifier(proof["response_id"])
        if response_id in self._response_ids:
            _bad("duplicate_model_response", stage=self._stage_name)
        fingerprint = proof["system_fingerprint"]
        if fingerprint is not None:
            DeepSeekRequiredClient._identifier(fingerprint)
        usage = DeepSeekRequiredClient._usage(proof["usage"])
        if strict_clean:
            tool_call_id = DeepSeekRequiredClient._identifier(proof["output_tool_call_id"])
            if tool_call_id in self._tool_call_ids:
                _bad("duplicate_model_tool_call", stage=self._stage_name)
            response = build_clean_safe_response(response_id=response_id, system_fingerprint=fingerprint,
                usage=usage, parsed=proof["parsed"], tool_call_id=tool_call_id)
        else:
            response = {"id": response_id, "model": MODEL, "system_fingerprint": fingerprint,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
                "content": json.dumps(proof["parsed"], ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False)}}], "usage": usage}
        if (not _same(usage, proof["usage"]) or not _same(response, proof["response"])
                or proof["response_sha256"] != _hash(response)):
            _bad("chat_response_identity_or_usage", stage=self._stage_name)
        self._response_ids.add(response_id)
        if strict_clean:
            self._tool_call_ids.add(tool_call_id)
        self._settle("chat", request, proof, usage)
        return deepcopy(proof["parsed"])

    def _complete_clean_responses(self, arguments, schema, output_policy):
        """Rebuild only the fixed native Responses contract, never a tool turn.

        Original native usage and explicit budget-normalized usage must agree.
        Responses completion is proved by its native response/message statuses;
        no Chat Completions finish reason is synthesized for this protocol.
        """
        stage = self._stage_name
        if stage != "clean.llm_semantic":
            _bad("responses_outside_clean_stage", stage=stage)
        slots_clean = output_policy == CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1
        body = (build_clean_slots_responses_request(**arguments) if slots_clean else
                build_clean_responses_request(**arguments))
        request, proof = self._consume("model.deepseek.chat", body,
            "tiger.deepseek-request.v3", "tiger.deepseek-call.v3",
            {"output_schema", "schema_sha256", "release", "output_policy_id"},
            {"requested_model", "response_id", "model", "release", "usage", "native_usage",
             "response_status", "created_at", "output_message_id", "response_sha256",
             "schema_sha256", "response", "search_mode", "parsed", "output_policy_id"},
            endpoint_path=CLEAN_OUTPUT_RESPONSES_PATH)
        if (request["release"] != RELEASE or proof["release"] != RELEASE
                or proof["requested_model"] != MODEL or proof["model"] != MODEL
                or request["output_policy_id"] != output_policy
                or proof["output_policy_id"] != output_policy
                or proof["search_mode"] != SEARCH_MODE
                or proof["response_status"] != "completed"
                or type(proof["created_at"]) is not int or proof["created_at"] <= 0
                or not _same(request["output_schema"], schema)
                or request["schema_sha256"] != _hash(schema)
                or proof["schema_sha256"] != request["schema_sha256"]):
            _bad("responses_contract", stage=stage)
        validate_schema(proof["parsed"], clean_slots_schema_v1(schema) if slots_clean else schema)
        if slots_clean:
            # Validate the exact recorded slot answer against every original
            # semantic constraint before returning it to clean_v2's decoder.
            # The safe response below remains the provider's actual slots.
            from .required import clean_slots_output_v1
            clean_slots_output_v1(proof["parsed"], schema, decode=True)
        if not _same(self._sanitizer._clean(proof["parsed"]), proof["parsed"]):
            _bad("unclean_responses_output", stage=stage)
        response_id = DeepSeekRequiredClient._identifier(proof["response_id"])
        message_id = DeepSeekRequiredClient._identifier(proof["output_message_id"])
        if response_id in self._response_ids or message_id in self._output_message_ids:
            _bad("duplicate_responses_identity", stage=stage)
        native_usage, usage = responses_usage(proof["native_usage"])
        response = build_clean_responses_safe_response(response_id=response_id,
            created_at=proof["created_at"], usage=native_usage,
            parsed=proof["parsed"], message_id=message_id)
        if (not _same(native_usage, proof["native_usage"])
                or not _same(usage, proof["usage"])
                or not _same(response, proof["response"])
                or proof["response_sha256"] != _hash(response)):
            _bad("responses_identity_or_native_usage", stage=stage)
        self._response_ids.add(response_id)
        self._output_message_ids.add(message_id)
        self._settle("chat", request, proof, usage)
        return deepcopy(proof["parsed"])


def _restore(value, *, native=False):
    """Restore only a closed local dataclass registry; never import a saved name."""
    from .. import data_models
    from ..agents.sector_agent import SectorEvidenceScore
    from ..search import models as search_models
    from ..tech_chain import agents as chain_agents
    from ..tech_chain import fusion
    classes = [getattr(data_models, name) for name in (
        "DimensionScore", "MRSResult", "SectorScore", "ChainLink", "ChainState", "StockCandidate", "TradePick", "PipelineResult")]
    classes += [getattr(search_models, name) for name in ("RawDocument", "Evidence", "CleanDocument", "CleanDocumentV2")]
    classes += [getattr(chain_agents, name) for name in (
        "LinkMomentum", "ChainMonitorRow", "GlobalLinkageRow", "SentimentRow", "SentimentEvidenceRow", "RiskAlert", "RiskAssessment")]
    classes += [fusion.TechChainSignal, fusion.TechChainEvidenceSignal, SectorEvidenceScore]
    registry = {cls.__module__ + "." + cls.__qualname__: cls for cls in classes}
    if isinstance(value, list):
        return [_restore(item, native=native) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("__type__") == "dataclass":
        cls = registry.get(value.get("class"))
        if cls is None or set(value) != {"__type__", "class", "fields"} or not isinstance(value["fields"], dict):
            _bad("unrecognized_typed_dataclass")
        if set(value["fields"]) != {field.name for field in fields(cls)}:
            _bad("typed_dataclass_fields")
        restored = {}
        for field in fields(cls):
            item = value["fields"][field.name]
            # Numeric dataclass fields came from actual numerical values.
            # Conversely, semantic dictionaries intentionally contain explicit
            # missing-value evidence objects, which must remain such objects.
            annotation = str(field.type).replace(" ", "")
            numeric = annotation in {"float", "Optional[float]", "float|None",
                "dict[str,float]", "dict[str,dict[str,float]]"}
            restored[field.name] = _restore(item, native=native or numeric)
        return cls(**restored)
    if native and value.get("__type__") == "missing":
        import pandas as pd
        reasons = {"nan": float("nan"), "positive_infinity": float("inf"),
                   "negative_infinity": -float("inf"), "pandas_na": pd.NA}
        if set(value) != {"__type__", "reason"} or value["reason"] not in reasons:
            _bad("numeric_missing_type")
        return reasons[value["reason"]]
    if native and value.get("__type__") in {"Timestamp", "datetime", "date"}:
        if set(value) != {"__type__", "value"}:
            _bad("numeric_timestamp_type")
        if value["__type__"] == "Timestamp":
            import pandas as pd
            return pd.Timestamp(value["value"])
        return (date if value["__type__"] == "date" else datetime).fromisoformat(value["value"])
    if native and value.get("__type__") == "dataframe":
        import pandas as pd
        keys = {"__type__", "index", "index_names", "columns", "column_names", "dtypes", "values"}
        dtypes = {"float64", "float32", "int64", "int32", "uint64", "uint32", "bool", "object"}
        if (set(value) != keys or not isinstance(value["dtypes"], list)
                or len(value["dtypes"]) != len(value["columns"])
                or any(dtype not in dtypes for dtype in value["dtypes"])):
            _bad("numeric_frame_type")
        frame = pd.DataFrame(_restore(value["values"], native=True),
            index=_restore(value["index"], native=True), columns=value["columns"])
        frame.index.names, frame.columns.names = value["index_names"], value["column_names"]
        frame = frame.astype(dict(zip(value["columns"], value["dtypes"])))
        if not _same(frame, value):
            _bad("numeric_frame_roundtrip")
        return frame
    if value.get("__type__") in {"dataframe", "series", "index", "missing", "Timestamp", "datetime", "date"}:
        return deepcopy(value)
    return {key: _restore(item, native=native) for key, item in value.items()}


def verify_required_model_v2(run, result, admission):
    """Verify all stage/source bindings, replay every semantic operation, return its summary."""
    try:
        return _verify(run, result, admission)
    except EvidenceInvalid:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OverflowError,
            LLMUnavailable, ExecutionContractError, RequiredModelFailed, StrictObservationError, BudgetRefused, RecursionError):
        _bad("invalid_or_missing_replay_evidence")


def _frozen_execution_policy(run, admission, preflight):
    """Choose a closed policy from sealed execution, never reported counters.

    Real admissions additionally validate the entire frozen source in the
    outer verifier. Minimal historical offline fixtures may omit policy.json;
    they can reconstruct only the original implicit 300k policy.
    """
    from ..agent_runtime import policy_manifest, verify_source
    from ..redline.execution import UPGRADE_POLICIES
    typed = preflight["policy"]
    if (not isinstance(typed, dict) or set(typed) != {"__type__", "class", "fields"}
            or typed["__type__"] != "dataclass"
            or typed["class"] != "trading_system.redline.execution.ExecutionPolicy"
            or not isinstance(typed["fields"], dict)):
        _bad("frozen_execution_policy_type", stage="daily.preflight")
    fields = deepcopy(typed["fields"])
    base = {"policy_id", "model_mode", "provider_names", "search_source_names", "market_timezone"}
    optional = {"budget_policy_id", "clean_output_policy_id"}
    if (not base.issubset(fields) or set(fields) - base - optional
            or any(fields[name] is None for name in optional if name in fields)):
        _bad("frozen_execution_policy_fields", stage="daily.preflight")
    for name in ("provider_names", "search_source_names"):
        if type(fields[name]) is not list:
            _bad("frozen_execution_policy_allowlist", stage="daily.preflight")
        fields[name] = tuple(fields[name])
    execution = ExecutionPolicy(**fields)
    if execution.clean_output_policy_id is not None and "source_sha256" not in admission:
        _bad("frozen_clean_source_binding", stage="daily.preflight")
    profiles = [profile for profile, row in UPGRADE_POLICIES.items() if row[0] == execution.policy_id]
    if len(profiles) != 1:
        _bad("frozen_execution_policy_profile", stage="daily.preflight")
    policy_file = safe_path(run, "policy.json", must_exist=False)
    if (policy_file.exists() or execution.budget_policy_id is not None
            or execution.clean_output_policy_id is not None or "policy_sha256" in admission):
        frozen = read_json(run, "policy.json")
        expected = policy_manifest(profiles[0], budget_policy_id=execution.budget_policy_id,
                                   clean_output_policy_id=execution.clean_output_policy_id)
        if (admission.get("policy_sha256") != digest(json_bytes(frozen))
                or not _same(frozen, expected)
                or not _same(frozen["execution"], execution.to_dict())):
            _bad("frozen_budget_policy_binding", stage="daily.preflight")
    if "source_sha256" in admission:
        from ..source_contract_v1 import STRICT_SOURCE_CONTRACT_V1
        verify_source(run, required_source_contract=(STRICT_SOURCE_CONTRACT_V1
            if execution.clean_output_policy_id is not None else None))
        if admission["source_sha256"] != digest(read_bytes(run, "source-manifest.json")):
            _bad("frozen_budget_source_binding", stage="daily.preflight")
    return execution


def _verify(run, result, admission):
    from .. import config
    from ..cleaning.pipeline import rule_base_clean
    from ..report import project_report
    from ..search.credibility import CrossValidator, scoring_docs
    from ..tech_chain.fusion import TechChainFusionAgent
    from ..tech_chain.universe import CHAIN_TOPICS, SECTOR_TOPICS
    from . import required as semantic
    from .candidate_research import research_candidates, validate_assessment

    clock = admission["clock"]
    assessment = validate_assessment({key: clock[key] for key in
        ("run_date", "market_timezone", "admitted_at_utc")})
    specs = daily_stage_contract(True, True)
    verify_events(run, run.name, specs)
    events = [strict_json(line) for line in read_bytes(run, "events.jsonl").splitlines()]
    ends = {row["stage"]: row for row in events if row["kind"] == "end"}
    if len(ends) != len(specs):
        _bad("duplicate_stage")
    inputs, outputs, calls = {}, {}, {}
    for spec in specs:
        stage, end = spec["name"], ends[spec["name"]]
        inputs[stage] = read_snapshot(run, end["input_ref"], run_id=run.name, stage=stage, role="input")
        outputs[stage] = read_snapshot(run, end["output_ref"], run_id=run.name, stage=stage, role="output")
        if stage in _COLLECTION_AND_MODEL:
            calls[stage] = []
        for call in outputs[stage]["io_calls"]:
            model_method = call["method"] in _MODEL_METHODS
            if stage in _COLLECTION_AND_MODEL and (not model_method or "error_ref" in call):
                _bad("unregistered_semantic_IO", stage=stage)
            if model_method:
                if stage not in _COLLECTION_AND_MODEL or "error_ref" in call:
                    _bad("model_IO_outside_its_stage", stage=stage)
                calls[stage].append({"method": call["method"],
                    "request": read_snapshot(run, call["input_ref"], run_id=run.name, stage=stage, role="input"),
                    "proof": read_snapshot(run, call["output_ref"], run_id=run.name, stage=stage, role="output")})
    execution = SimpleNamespace(run_date=date.fromisoformat(assessment["run_date"]),
        policy=_frozen_execution_policy(run, admission, inputs["daily.preflight"]),
        upgrade=True, admitted_at_utc=assessment["admitted_at_utc"], deadline_at_utc=clock.get("deadline_at_utc"))
    if not _same(inputs["pipeline.prepare"].get("policy"), normalize(execution.policy)):
        _bad("pipeline_frozen_execution_policy_binding", stage="pipeline.prepare")
    client = ReplayClient(execution, calls)

    def equal(actual, expected, reason, stage):
        if not _same(actual, expected):
            _bad(reason, stage=stage)

    def value(stage):
        return outputs[stage]["value"]

    def pure_stage(stage):
        if outputs[stage]["io_calls"]:
            _bad("unexpected_deterministic_stage_IO", stage=stage)

    expected_search_input = {"chains": CHAIN_TOPICS, "sectors": SECTOR_TOPICS,
        "ah": config.AH_TOPICS, "ah_enabled": False, "limit_per_query": 4,
        "deep": True, "source_allowlist": ["deepseek_search"]}
    equal(inputs["search.collect"], expected_search_input, "broad_collection_inputs", "search.collect")
    client.set_stage("search.collect")
    collected = semantic.collect_v2(client, {**CHAIN_TOPICS, **SECTOR_TOPICS}, assessment["run_date"])
    client.finish_stage()
    equal(value("search.collect"), {"documents": collected, "topics": {**CHAIN_TOPICS, **SECTOR_TOPICS},
        "batches": [], "cleanup": {"native_calls_completed": len(client.search_receipts)}},
        "broad_collection_projection", "search.collect")
    pure_stage("clean.rule_base")
    equal(inputs["clean.rule_base"], {"documents": collected}, "rule_clean_input", "clean.rule_base")
    base = rule_base_clean(collected)
    equal(value("clean.rule_base"), {"documents": base}, "rule_clean_output", "clean.rule_base")
    equal(inputs["clean.llm_semantic"], {"documents": base, "assessment": assessment},
          "semantic_clean_input", "clean.llm_semantic")
    client.set_stage("clean.llm_semantic")
    cleaned = semantic.clean_v2(client, base)
    client.finish_stage()
    equal(value("clean.llm_semantic"), {"documents": cleaned, "degraded_count": 0},
          "semantic_clean_output", "clean.llm_semantic")
    pure_stage("clean.cross_validate")
    equal(inputs["clean.cross_validate"], {"documents": cleaned}, "cross_validation_input", "clean.cross_validate")
    stats = CrossValidator(contract_version=2).validate(cleaned)
    scored = scoring_docs(cleaned, contract_version=2)
    equal(value("clean.cross_validate"), {"documents": cleaned, "scoring_documents": scored, "stats": stats},
          "cross_validation_output", "clean.cross_validate")
    semantic_values = {}
    for stage, docs, operation in (
        ("tech.sentiment", scored, lambda: semantic.sentiment_v2(client, scored)),
        ("tech.risk", cleaned, lambda: semantic.risk_v2(client, cleaned)),
        ("sector.narrative", scored, lambda: semantic.narrative_v2(client, scored, inputs["sector.narrative"]["sector_symbols"])),
    ):
        expected_input = {"documents": docs, "assessment": assessment}
        if stage == "sector.narrative":
            sector_symbols = inputs["pipeline.prepare"].get("market")
            # The prepare output is the authoritative market specification.
            sector_symbols = value("pipeline.prepare")["market"]["sector_symbols"]
            expected_input["sector_symbols"] = sector_symbols
        equal(inputs[stage], expected_input, "semantic_stage_inputs", stage)
        client.set_stage(stage)
        semantic_values[stage] = operation()
        client.finish_stage()
        expected_output = ({"raw_result": semantic_values[stage], "narrative": semantic_values[stage]}
                           if stage == "sector.narrative" else semantic_values[stage])
        equal(value(stage), expected_output, "semantic_stage_output", stage)
    expected_fusion = {"monitor": value("tech.monitor"), "linkage": value("tech.cycle_linkage"),
        "sentiment": semantic_values["tech.sentiment"], "alerts": semantic_values["tech.risk"]}
    equal(inputs["tech.fusion"], expected_fusion, "fusion_semantic_inputs", "tech.fusion")
    pure_stage("tech.fusion")
    signals = TechChainFusionAgent().execute(_restore(value("tech.monitor")), _restore(value("tech.cycle_linkage")),
        semantic_values["tech.sentiment"], semantic_values["tech.risk"])
    equal(value("tech.fusion"), signals, "fusion_full_evidence_output", "tech.fusion")
    equal(inputs["layer2.sector"], {"market_data": value("data.prepare")["market_data"],
        "narrative": semantic_values["sector.narrative"]}, "sector_narrative_binding", "layer2.sector")
    for sector in _restore(value("layer2.sector")["sectors"]):
        expected_narrative = semantic_values["sector.narrative"].get(sector.etf, {})
        equal(sector.semantic_evidence, expected_narrative, "full_sector_narrative", "layer2.sector")
        score = expected_narrative.get("score")
        equal(sector.factors.get("narr"), None if score is None else round(score, 1), "sector_semantic_score", "layer2.sector")

    context = _bound_context(run, inputs, outputs, assessment, semantic_values, signals)
    equal(inputs["research.candidates"], {"context": context, "documents": cleaned},
          "issuer_research_complete_inputs", "research.candidates")
    client.set_stage("research.candidates")
    research = research_candidates(client, context, cleaned)
    client.finish_stage()
    equal(value("research.candidates"), research, "issuer_research_complete_output", "research.candidates")
    expected_decision_context = {**context, "candidate_research": research}
    account = context["portfolio_snapshot"]["nav"]
    expected_decision_input = {"enabled": config.DEBATE_ENABLED, "picks": context["picks"],
        "rationale": context["pick_rationale"], "watchlist": context["watchlist"],
        "action": context["action"], "documents": cleaned, "context": expected_decision_context,
        "account_usd": account, "assessment": assessment}
    equal(inputs["decision.debate"], expected_decision_input, "decision_complete_inputs", "decision.debate")
    decision_context = deepcopy(expected_decision_context)
    client.set_stage("decision.debate")
    decision = semantic.decision_v2(client, decision_context, cleaned, account)
    client.finish_stage()
    equal(value("decision.debate"), decision, "decision_complete_output", "decision.debate")
    client.finish()
    summary = semantic.model_summary_v2(client)
    raw = result["raw"]
    expected_raw = {"model": summary, "action_summary": decision_context["action_summary"],
        "execution_decisions": decision_context["execution_decisions"], "candidate_research": research,
        "semantic_evidence": context["semantic_evidence"], "portfolio_snapshot": context["portfolio_snapshot"],
        "portfolio_marks": context["portfolio_marks"], "execution_setups": context["execution_setups"],
        "cohort_inputs": context["cohort_inputs"], "policy_identity": context["policy_identity"],
        "numeric_approved_count": len(context["picks"]), "numeric_approved_picks": context["picks"],
        "account_usd": account, "execution_policy": "whitepaper-execution-v2", "debate": decision}
    expected_raw_plain, _ = project_report(expected_raw)
    for key, expected in expected_raw_plain.items():
        equal(raw.get(key), expected, "published_semantic_field_" + key, "report.emit")
    expected_picks, _ = project_report(decision_context["picks"])
    equal(result.get("picks"), expected_picks, "published_model_veto_picks", "report.emit")
    equal(result.get("action"), decision_context["action"], "published_model_action", "report.emit")
    equal(result.get("market_view"), decision_context["market_view"], "published_model_summary", "report.emit")
    emitted = _restore(value("report.emit"))
    equal(emitted.picks, decision_context["picks"], "emitted_model_veto_picks", "report.emit")
    equal(emitted.action, decision_context["action"], "emitted_model_action", "report.emit")
    equal(emitted.market_view, decision_context["market_view"], "emitted_model_summary", "report.emit")
    for key, expected in expected_raw.items():
        equal(emitted.raw.get(key), expected, "emitted_semantic_field_" + key, "report.emit")
    _verify_tail(run, result, admission, inputs, outputs, ends)
    return summary


def _bound_context(run, inputs, outputs, assessment, semantic_values, signals):
    """Build all consumed research facts from their registered upstream owners."""
    from ..chains import TICKER_TO_CHAIN
    from ..execution_policy import portfolio_snapshot
    from ..tech_chain.fusion import signals_to_bonus_map
    from ..workflow_v2 import account_symbols
    from .market_evidence import market_snapshots

    value = lambda stage: outputs[stage]["value"]
    data, prepare = value("data.prepare"), value("pipeline.prepare")
    market = data["market_data"]
    state = inputs["pipeline.prepare"]["portfolio_state"]
    mrs, sectors, chains = value("layer1.mrs"), value("layer2.sector"), value("layer2b.chain")
    risk, risk_input = value("layer4.risk"), inputs["layer4.risk"]

    def equal(actual, expected, reason):
        if not _same(actual, expected):
            _bad(reason, stage="research.candidates")

    equal(inputs["layer1.mrs"], {"market_data": market}, "MRS_market_binding")
    equal(inputs["layer2b.chain"], {"spy": market["spy"], "stock_ohlcv": market["stock_ohlcv"]}, "chain_market_binding")
    original_watch = value("layer3.tss")["watchlist"]
    equal(risk_input["watchlist"], original_watch, "risk_TSS_input")
    equal(risk_input["mrs"], mrs, "risk_MRS_input")
    equal(risk_input["sectors"], sectors["sectors"], "risk_sector_input")
    equal(risk_input["chains"], chains["chains"], "risk_chain_input")
    equal(risk_input["portfolio_state"], state, "risk_original_account")
    equal(risk_input["trade_date"], assessment["run_date"], "risk_research_date")
    equal(risk_input["market_grad"], prepare["market_grad"], "risk_market_graduation")
    equal(risk_input["market"], prepare["market"], "risk_market_spec")
    equal(risk_input["calendar"], prepare["calendar"], "risk_calendar_snapshot")
    numeric = _replay_numeric_risk(risk_input, market, chains["chain_map"])
    for key, expected_value in numeric.items():
        equal(risk.get(key), expected_value, "canonical_risk_output_" + key)
    expected_marks = {row["symbol"]: {"date": row["as_of"], "price": row["close"]}
        for row in market_snapshots(market, account_symbols(state),
            {key: assessment[key] for key in ("run_date", "market_timezone")})}
    valuation_date = market["freshness"]["benchmark_last_bar"]
    snapshot = portfolio_snapshot(state, expected_marks, valuation_date)
    equal(risk_input["portfolio_marks"], expected_marks, "risk_current_account_marks")
    equal(risk_input["portfolio_snapshot"], snapshot, "risk_current_NAV_snapshot")
    equal(risk["portfolio_marks"], expected_marks, "research_current_account_marks")
    equal(risk["portfolio_snapshot"], snapshot, "research_current_NAV_snapshot")
    equal(risk_input["account_usd"], snapshot["nav"], "risk_current_NAV_amount")
    if risk_input["execution_policy"] != "whitepaper-execution-v2" or not 1 <= risk_input["max_picks"] <= 10:
        _bad("risk_v2_policy_or_cap", stage="layer4.risk")
    # The only risk-owned mutations of a TSS candidate are its resolved sector
    # and final TOS. This cannot authorize dropping any field or candidate.
    research_context = inputs["research.candidates"]["context"]
    observed_watch = research_context["watchlist"]
    if len(original_watch) != len(observed_watch):
        _bad("risk_watch_coverage", stage="layer4.risk")
    for original, changed in zip(original_watch, observed_watch):
        if (not isinstance(original, dict) or not isinstance(changed, dict)
                or set(original) != set(changed) or set(original["fields"]) != set(changed["fields"])):
            _bad("risk_watch_structure", stage="layer4.risk")
        equal({key: val for key, val in original.items() if key != "fields"},
              {key: val for key, val in changed.items() if key != "fields"}, "risk_watch_type")
        equal({key: val for key, val in original["fields"].items() if key not in {"sector_etf", "tos"}},
              {key: val for key, val in changed["fields"].items() if key not in {"sector_etf", "tos"}}, "risk_watch_facts")
    _policy_identity(run, outputs["layer4.risk"]["io_calls"], risk["policy_identity"])
    if risk["cohort_inputs"].get("policy_hash") != risk["policy_identity"]["sha256"]:
        _bad("cohort_policy_identity", stage="layer4.risk")
    equal(risk["cohort_inputs"].get("portfolio_state"), state, "cohort_original_account")
    equal(risk["cohort_inputs"].get("portfolio_snapshot"), snapshot, "cohort_original_snapshot")
    equal(risk["cohort_inputs"].get("setups"), risk["execution_setups"], "cohort_original_setups")
    equal(risk["cohort_inputs"].get("budget"), risk["entry_budget"], "cohort_original_budget")
    signal_map = {row.main_chain: row for row in signals}
    mapped = sum(1 for ticker in data["all_tickers"] if ticker in TICKER_TO_CHAIN)
    total = len(data["all_tickers"])
    expected = {"market_data": market, "trade_date": assessment["run_date"],
        "chain_coverage": f"{mapped}/{total}（{mapped / max(total, 1):.0%}）",
        "scan_filters": prepare["scan_filters"], "market_grad": prepare["market_grad"],
        "event_calendar": prepare["calendar"],
        "tech_signals": [row.to_dict() for row in signals], "tech_bonus_map": signals_to_bonus_map(signals),
        "tech_signal_map": signal_map, "narrative_llm": semantic_values["sector.narrative"],
        "paper_execution_budgeted": True, "portfolio_state": state,
        "execution_policy": "whitepaper-execution-v2", "research_contract_version": 2,
        "research_holdings": [{**row, "symbol": row["ticker"]} for row in state.get("positions", [])],
        "semantic_evidence": {"sentiment": semantic_values["tech.sentiment"], "risk": semantic_values["tech.risk"],
                              "narrative": semantic_values["sector.narrative"]},
        "mrs": mrs, "sectors": sectors["sectors"], "sector_map": sectors["sector_map"],
        "chains": chains["chains"], "chain_map": chains["chain_map"],
        "scan_stats": value("layer0.scan")["scan_stats"], "watchlist": observed_watch, **risk}
    equal(risk_input["tech_signal_map"], signal_map, "risk_full_tech_evidence")
    equal(research_context, expected, "complete_upstream_context")
    return _restore(normalize(expected))


def _replay_numeric_risk(risk_input, market, chain_map):
    """Recompute canonical gates and sizing; recorded outputs are not authority."""
    from ..agents.risk_manager_agent import RiskManagerAgent
    from ..events import EventCalendar
    from ..markets import get_market
    spec = get_market("us")
    if not _same(spec.to_dict(), risk_input["market"]):
        _bad("unsupported_numeric_market", stage="layer4.risk")
    calendar = EventCalendar.__new__(EventCalendar)
    frozen = risk_input["calendar"]
    if (not isinstance(frozen, dict) or set(frozen) != {"macro", "earnings", "override_path", "earnings_source"}
            or any(not isinstance(frozen[group], dict) for group in ("macro", "earnings"))):
        _bad("frozen_numeric_calendar", stage="layer4.risk")
    calendar._macro, calendar._earnings = deepcopy(frozen["macro"]), deepcopy(frozen["earnings"])
    calendar.override_path = frozen["override_path"]
    context = _restore({**risk_input, "chain_map": chain_map}, native=True)
    # Risk reads complete own-stock Close references only. Other provider data
    # remains typed evidence rather than being coerced into new observations.
    context.update(market_data={"stock_ohlcv": _restore(market["stock_ohlcv"], native=True)},
                   market_spec=spec, event_calendar=calendar)
    RiskManagerAgent(None, account_usd=risk_input["account_usd"],
                     max_picks=risk_input["max_picks"]).execute(context)
    return {key: context[key] for key in ("picks", "pick_rationale", "action", "market_view", "notes",
        "currency_policy", "compliance", "rejections", "entry_budget", "execution_setups", "portfolio_snapshot",
        "portfolio_marks", "gross_cap", "tranche_confirmations", "ranked_allocations")}


def _policy_identity(run, calls, identity):
    names = ("execution_policy.py", "exit_engine.py", "simulator.py", "config.py", "gate.py", "currency.py", "calendar.py")
    expected = {"schema": "tiger.execution-policy-source.v2", "execution_policy": "whitepaper-execution-v2",
        "files": [{"path": "trading_system/" + name,
                   "sha256": hashlib.sha256(read_bytes(run, "source/trading_system/" + name)).hexdigest()} for name in names]}
    expected["sha256"] = _hash(expected)
    if not _same(identity, expected) or len(calls) != 1 or calls[0]["method"] != "execution.policy.identity" or "error_ref" in calls[0]:
        _bad("frozen_execution_policy_sources", stage="layer4.risk")
    call = calls[0]
    request = read_snapshot(run, call["input_ref"], run_id=run.name, stage="layer4.risk", role="input")
    response = read_snapshot(run, call["output_ref"], run_id=run.name, stage="layer4.risk", role="output")
    if not _same(request, {"execution_policy": "whitepaper-execution-v2"}) or not _same(response, expected):
        _bad("policy_source_IO_binding", stage="layer4.risk")


def _provider_call(run, stage, call, allowed_names, methods):
    """Closed production provider framing, including availability probes."""
    parts = call["method"].split(".")
    if len(parts) != 3 or parts[0] != "provider" or parts[1] not in allowed_names or parts[2] not in methods:
        _bad("unregistered_provider_IO", stage=stage)
    name, method = parts[1:]
    request = read_snapshot(run, call["input_ref"], run_id=run.name, stage=stage, role="input")
    if method == "available":
        channels = {"official", "tencent", "sina", "eastmoney", "agentgw", "ifind_gw", "tiingo"}
        if name not in channels or request != {"provider": name}:
            _bad("provider_availability_request", stage=stage)
    elif (not isinstance(request, dict) or set(request) != {"provider", "method", "args", "kwargs"}
            or request["provider"] != name or request["method"] != method
            or not isinstance(request["args"], list) or len(request["args"]) != 1
            or not isinstance(request["args"][0], str)
            or request["kwargs"] != ({"days": 400} if method == "ohlcv" else {})):
        _bad("provider_observation_request", stage=stage)
    failed = "error_ref" in call
    value = read_snapshot(run, call["error_ref" if failed else "output_ref"],
                          run_id=run.name, stage=stage, role="output")
    if failed:
        if (not isinstance(value, dict) or set(value) != {"error_type", "message", "outcome_observed"}
                or value["message"] != "External call failed" or value["outcome_observed"] is not True
                or not isinstance(value["error_type"], str)
                or value["error_type"] in {"EvidenceWriteError", "ExecutionContractError", "StrictObservationError"}):
            _bad("fatal_or_invalid_provider_failure", stage=stage)
    elif method == "available" and type(value) is not bool:
        _bad("provider_availability_response", stage=stage)
    return name, method, request, value, failed


def _verify_tail(run, result, admission, inputs, outputs, ends):
    """Replay continuity and report-only cohorts without saving or fetching.

    Registered load/clock/quote/history snapshots are the only observations.
    The ledger's actual pure transitions and shared numeric policy run again;
    saved flags and three mutually equal report copies are not authority.
    """
    from zoneinfo import ZoneInfo
    from ..business_evaluation import build_business_evaluation, mature_cohorts
    from ..calendar import next_trading_day
    from ..execution_policy import allocate_entry
    from ..opportunity_lifecycle import OpportunityLedger, quote_refusal_reason
    from ..report import project_report
    from ..workflow_runtime_v2 import _current_snapshot, _evidence_refs

    _verify_paper(run, admission, inputs, outputs)
    provider_names = inputs["daily.preflight"]["policy"]["fields"]["provider_names"]

    def equal(actual, expected, reason, stage="opportunity.lifecycle"):
        if not _same(actual, expected):
            _bad(reason, stage=stage)

    def io(stage, method):
        rows = []
        allowed = ({"opportunity.load", "clock.observe", "opportunity.quote", "opportunity.save", "workflow.artifact.write"}
                   if stage == "opportunity.lifecycle" else
                   {"opportunity.load", "clock.observe", "evaluation.history", "opportunity.save", "workflow.artifact.write"})
        for call in outputs[stage]["io_calls"]:
            if call["method"] not in allowed and not call["method"].startswith("provider."):
                _bad("unexpected_continuity_IO", stage=stage)
            if call["method"].startswith("provider."):
                _provider_call(run, stage, call, provider_names,
                    {"available", "quote" if stage == "opportunity.lifecycle" else "ohlcv"})
            if call["method"] == method:
                if "error_ref" in call:
                    _bad("failed_required_continuity_IO", stage=stage)
                rows.append((read_snapshot(run, call["input_ref"], run_id=run.name, stage=stage, role="input"),
                    read_snapshot(run, call["output_ref"], run_id=run.name, stage=stage, role="output")))
        return rows

    def one(stage, method):
        rows = io(stage, method)
        if len(rows) != 1:
            _bad("continuity_IO_count_" + method, stage=stage)
        return rows[0]

    def path(name):
        return {"__type__": "path", "value": str(run / "work" / "reports" / name)}

    def book(snapshot):
        OpportunityLedger._validate(snapshot)
        if snapshot["account_kind"] != "paper":
            _bad("continuity_account_namespace")
        ledger = OpportunityLedger.__new__(OpportunityLedger)
        ledger.account_kind, ledger._state, ledger._dirty = "paper", deepcopy(snapshot), False
        return ledger

    def save(stage, ledger):
        request, actual = one(stage, "opportunity.save")
        equal(request, {"path": path("opportunity_ledger.json"), "before": ledger.snapshot()}, "ledger_save_inputs", stage)
        expected = ledger.snapshot()
        if ledger._dirty:
            expected["revision"] += 1
        OpportunityLedger._validate(expected)
        equal(actual, expected, "ledger_pure_transition_and_revision", stage)
        ledger._state, ledger._dirty = deepcopy(expected), False
        return expected

    def artifacts(stage, expected):
        rows = io(stage, "workflow.artifact.write")
        equal([request for request, _ in rows], [{"path": path(name), "value": value}
            for name, value in expected.items()], "workflow_artifact_requests", stage)
        for (_, actual), value in zip(rows, expected.values()):
            equal(actual, value, "workflow_artifact_readback", stage)

    def observations(stage, outer_method, provider_method):
        """An orchestration wrapper cannot invent a separate provider result."""
        import math
        from ..redline.execution import complete_daily_cutoff, market_observation
        calls = outputs[stage]["io_calls"]
        covered = set()
        for index, outer in enumerate(calls):
            if outer["method"] != outer_method:
                continue
            request = read_snapshot(run, outer["input_ref"], run_id=run.name, stage=stage, role="input")
            actual = read_snapshot(run, outer["output_ref"], run_id=run.name, stage=stage, role="output")
            candidates = []
            for inner_index, inner in enumerate(calls[index + 1:], index + 1):
                if not inner["method"].startswith("provider."):
                    break
                covered.add(inner_index)
                if not inner["method"].endswith("." + provider_method) or "error_ref" in inner:
                    continue
                observed_input = read_snapshot(run, inner["input_ref"], run_id=run.name, stage=stage, role="input")
                if (observed_input.get("args") or [None])[0] != request["symbol"]:
                    _bad("provider_observation_symbol_binding", stage=stage)
                observed = read_snapshot(run, inner["output_ref"], run_id=run.name, stage=stage, role="output")
                if provider_method == "quote":
                    candidates.extend([observed, {**observed, "stale": True}] if isinstance(observed, dict) else [observed])
                elif isinstance(observed, dict) and observed.get("__type__") == "dataframe":
                    frame = market_observation(_restore(observed, native=True),
                        run_date=date.fromisoformat(admission["clock"]["run_date"]), market_timezone="America/New_York")
                    cutoff = complete_daily_cutoff(date.fromisoformat(admission["clock"]["run_date"]), admission["clock"]["admitted_at_utc"])
                    candidates.append(frame[frame.index.date <= cutoff])
            if provider_method == "quote" and actual is None:
                if any(isinstance(row, dict) and type(row.get("price")) in (int, float)
                       and math.isfinite(row["price"]) and row["price"] > 0 for row in candidates):
                    _bad("unexplained_dropped_provider_quote", stage=stage)
            elif not any(_same(actual, row) for row in candidates):
                _bad("actual_provider_observation_binding", stage=stage)
        if covered != {index for index, call in enumerate(calls) if call["method"].startswith("provider.")}:
            _bad("orphan_provider_observation", stage=stage)

    # Bind the sizing account to the actual initial accounting state, and the
    # continuation account to the actual post-paper state and persisted bytes.
    paper = outputs["paper.persist"]["value"]["state"]
    equal(inputs["pipeline.prepare"]["portfolio_state"], outputs["daily.preflight"]["value"]["sim"], "sizing_account_preflight")
    equal(inputs["opportunity.lifecycle"]["paper_state"], paper, "continuity_post_paper_account")
    equal(read_json(run, "work/reports/sim_portfolio.json"), paper, "persisted_post_paper_account")
    life_result = _restore(inputs["opportunity.lifecycle"]["result"])
    expected_final = deepcopy(result)
    for key in ("opportunity_lifecycle", "lifecycle_observation", "current_portfolio_snapshot", "business_evaluation", "serialization"):
        expected_final["raw"].pop(key, None)
    equal(project_report(life_result)[0], expected_final, "continuity_complete_model_result_input")
    equal(inputs["opportunity.lifecycle"], {"result": life_result, "paper_state": paper, "run_id": run.name}, "continuity_complete_stage_inputs")
    loaded_request, loaded = one("opportunity.lifecycle", "opportunity.load")
    equal(loaded_request, {"path": path("opportunity_ledger.json")}, "ledger_load_path")
    before_path = safe_path(run, "account-before/opportunity_ledger.json", must_exist=False)
    before = read_json(run, "account-before/opportunity_ledger.json") if before_path.exists() else OpportunityLedger.initial_state()
    equal(loaded, before, "ledger_admitted_account_before_state")
    ledger = book(loaded)
    clocks = io("opportunity.lifecycle", "clock.observe")
    equal([request for request, _ in clocks], [{"purpose": "research_result_ready"},
        {"purpose": "quotes_observed_after_research"}], "continuity_clock_purposes")
    ready, observed = [value for _, value in clocks]
    admitted = _instant(admission["clock"]["admitted_at_utc"])
    if not admitted <= _instant(ready) <= _instant(observed):
        _bad("continuity_actual_clock_order")
    expires = datetime.combine(next_trading_day(life_result.trade_date), time(16), ZoneInfo("America/New_York")).isoformat()
    ledger.record_research(life_result, run_id=run.name, observed_at=ready, expires_at=expires)
    ledger.sync_account(paper, snapshot_id=run.name + ":paper-after", observed_at=ready, account_kind="paper")
    symbols = list(dict.fromkeys(row["ticker"] for row in ledger.snapshot()["opportunities"]
        if row["state"] not in {"CLOSED", "INVALIDATED", "EXPIRED", "SUPERSEDED"}))
    quote_rows = io("opportunity.lifecycle", "opportunity.quote")
    observations("opportunity.lifecycle", "opportunity.quote", "quote")
    equal([request for request, _ in quote_rows], [{"symbol": symbol} for symbol in symbols], "all_active_quote_requests")
    quotes = {symbol: value for symbol, (_, value) in zip(symbols, quote_rows)}
    alerts = ledger.observe_quotes(quotes, observed_at=observed)
    snapshot = _current_snapshot(life_result, paper)
    snapshot["frozen_at"] = observed
    approved = {pick.ticker: pick for pick in life_result.picks}
    decisions = {row["symbol"]: row for row in life_result.raw["execution_decisions"]}
    execution = SimpleNamespace(summary=lambda: [{"stage": name, "status": row["status"], "output_ref": row["output_ref"]}
        for name, row in ends.items()])
    rechecks = []
    for alert in alerts:
        if alert["kind"] != "REQUALIFY":
            continue
        symbol, quote = alert["ticker"], quotes[alert["ticker"]]
        pick = approved.get(symbol)
        commitments = [identity for identity, row in snapshot["reservations"].items() if row["ticker"] == symbol]
        if commitments:
            rechecks.append({"opportunity_id": alert["opportunity_id"], "symbol": symbol,
                "accepted": False, "status": "account_execution_recheck_required",
                "reasons": [alert.get("reason") or "ACCOUNT_ENTRY_ALREADY_PENDING"],
                "account_plan_ids": commitments, "allocation": None,
                "current_research_approved": pick is not None and decisions.get(symbol, {}).get("action") == "buy",
                "risk_evidence_ids": _evidence_refs(execution, quote), "requires_execution_recheck": True})
            continue
        reasons, plan, allocation = [], None, None
        if pick is None or decisions.get(symbol, {}).get("action") != "buy":
            reasons.append("CURRENT_COMPLETE_MODEL_AND_NUMERIC_APPROVAL_REQUIRED")
        else:
            refusal = quote_refusal_reason(quote, observed_at=observed)
            if refusal:
                reasons.append(refusal)
            else:
                setup = deepcopy(life_result.raw["execution_setups"][symbol])
                setup["entry_price"] = quote["price"]
                budget = {**life_result.raw["entry_budget"], "snapshot_hash": snapshot["state_hash"],
                    "execution_snapshot": snapshot, "admission_snapshot_hash": life_result.raw["entry_budget"]["snapshot_hash"]}
                allocation = allocate_entry(snapshot, setup, budget, pick.shares, exclude_plan_id=None)
                if not allocation["shares"]:
                    reasons.append(allocation["reason"])
                else:
                    plan = {**asdict(pick), "shares": allocation["shares"], "entry_price": quote["price"],
                        "risk_usd": allocation["risk_usd"], "position_pct": allocation["position_pct"],
                        "quote_evidence_id": alert["evidence_id"], "qualification_known_at": observed,
                        "execution_policy": "whitepaper-execution-v2"}
        accepted = not reasons
        ledger.requalify(alert["opportunity_id"], run_id=run.name, observed_at=observed, accepted=accepted,
            reasons=reasons, risk_evidence_ids=_evidence_refs(execution, quote) if isinstance(quote, dict) else [],
            account_snapshot=snapshot, plan=plan, assessment_date=snapshot["known_at"])
        rechecks.append({"opportunity_id": alert["opportunity_id"], "symbol": symbol, "accepted": accepted,
                        "reasons": reasons, "allocation": allocation, "requires_execution_recheck": True})
    after_life = save("opportunity.lifecycle", ledger)
    expected_life = {"lifecycle": after_life, "quotes": quotes, "alerts": alerts, "requalifications": rechecks,
        "current_portfolio_snapshot": snapshot, "research_ready_at": ready, "observed_at": observed,
        "artifact": str(run / "work/reports/opportunity-lifecycle.json")}
    equal(outputs["opportunity.lifecycle"]["value"], expected_life, "complete_continuity_pure_replay")
    artifacts("opportunity.lifecycle", {"opportunity-lifecycle.json": after_life})
    life_result.raw.update(opportunity_lifecycle=after_life, current_portfolio_snapshot=snapshot,
        lifecycle_observation={"research_ready_at": ready, "observed_at": observed, "alerts": alerts,
            "requalifications": rechecks,
            "execution_boundary": "Provisional paper/manual coordination only; no broker submission or invented execution."})
    equal(inputs["business.evaluate"], {"result": life_result, "paper_state": paper,
        "lifecycle": after_life, "run_id": run.name}, "evaluation_complete_stage_inputs", "business.evaluate")
    evaluation_load, evaluation_before = one("business.evaluate", "opportunity.load")
    equal(evaluation_load, {"path": path("opportunity_ledger.json")}, "evaluation_ledger_load_path", "business.evaluate")
    equal(evaluation_before, after_life, "evaluation_ledger_load_before_state", "business.evaluate")
    clock_request, as_of = one("business.evaluate", "clock.observe")
    equal(clock_request, {"purpose": "business_evaluation"}, "evaluation_actual_clock_purpose", "business.evaluate")
    if _instant(as_of) < _instant(observed) or _instant(as_of) >= _instant(admission["clock"]["deadline_at_utc"]):
        _bad("evaluation_actual_clock_order", stage="business.evaluate")
    histories = set()
    terminal = {row["opportunity_id"] for row in after_life["cohort_outcomes"] if row.get("horizon_complete")}
    for row in after_life["opportunities"]:
        if (row.get("source_run_id") is not None and row["source_run_id"] != run.name
                and row["opportunity_id"] not in terminal and row.get("signal_date")
                and row["signal_date"] < admission["clock"]["run_date"]):
            histories.add(row["ticker"])
            frozen = next((item.get("cohort_inputs") for item in after_life["research_runs"] if item["run_id"] == row["source_run_id"]), None)
            if isinstance(frozen, dict):
                state = frozen.get("portfolio_state", {})
                histories.update(item["ticker"] for item in state.get("positions", []) + state.get("pending", []))
                histories.update(item["ticker"] for item in state.get("entry_plans", {}).values() if item["status"] in {"pending", "partial"})
                histories.update(setup["sector"] for setup in frozen.get("setups", {}).values() if setup.get("sector"))
    history_rows = io("business.evaluate", "evaluation.history")
    observations("business.evaluate", "evaluation.history", "ohlcv")
    equal([request for request, _ in history_rows], [{"symbol": symbol} for symbol in sorted(histories)], "all_cohort_history_requests", "business.evaluate")
    bars = {symbol: value for symbol, (_, value) in zip(sorted(histories), history_rows)}
    outcomes = mature_cohorts(after_life, _restore(bars, native=True), as_of,
                            current_policy_hash=life_result.raw["policy_identity"]["sha256"])
    ledger.record_outcomes(outcomes, observed_at=as_of)
    final_ledger = save("business.evaluate", ledger)
    evaluation = build_business_evaluation(life_result, lifecycle=final_ledger, account_state=paper, as_of=as_of, run_id=run.name)
    expected_evaluation = {"evaluation": evaluation, "lifecycle": final_ledger, "market_bars": bars,
        "outcomes": outcomes, "as_of": as_of, "artifacts": {
            "lifecycle": str(run / "work/reports/opportunity-lifecycle.json"),
            "evaluation": str(run / "work/reports/business-evaluation.json")}}
    equal(outputs["business.evaluate"]["value"], expected_evaluation, "complete_evaluation_pure_replay", "business.evaluate")
    artifacts("business.evaluate", {"opportunity-lifecycle.json": final_ledger, "business-evaluation.json": evaluation})
    life_result.raw.update(opportunity_lifecycle=final_ledger, business_evaluation=evaluation)
    expected, missing = project_report(life_result)
    expected["raw"]["serialization"] = {"schema": "tiger.report-json.v1", "nonfinite_values": missing}
    equal(result, expected, "complete_final_business_result", "daily.result")


def _verify_paper(run, admission, inputs, outputs):
    """Recompute fills, exits, friction and cash from actual recorded bars.

    A persisted account and all its report copies can agree on an invented
    transition. Only the original admitted account, model-vetted research and
    dated provider observations may determine the canonical paper transition.
    """
    import pandas as pd
    from ..currency import require_usd_result
    from ..execution_policy import step_paper_account
    from ..pipeline import _missing_data, _series_stale
    from ..redline import DEFERRED_STEPS, pipeline_registry
    from ..redline.execution import complete_daily_cutoff, market_observation
    from ..simulator import Bar, SimEngine

    def equal(actual, expected, reason, stage="paper.step"):
        if not _same(actual, expected):
            _bad(reason, stage=stage)

    day = date.fromisoformat(admission["clock"]["run_date"])
    cutoff = complete_daily_cutoff(day, admission["clock"]["admitted_at_utc"])
    before = outputs["daily.preflight"]["value"]["sim"]
    emitted = _restore(outputs["report.emit"]["value"])
    policy = require_usd_result(emitted, scope="严格日报交易记账")
    equal(outputs["accounting.validate"]["value"]["policy"], policy, "canonical_accounting_policy")
    equal(outputs["accounting.validate"]["value"]["sim"], before, "accounting_original_paper_state")
    emitted.raw["accounting"] = {**policy, "status": "pending"}
    # The tracer appends display-only elapsed times after report.emit. These
    # times are not recorded in strict events and cannot be reconstructed.
    # Validate their closed shape and executed stage identities explicitly.
    trace = inputs["paper.step"]["result"]["fields"]["raw"]["redline"]
    specs = pipeline_registry(True)
    equal([row["step"] for row in trace], [spec.name for spec in specs], "paper_redline_stage_identity")
    for row in trace:
        if (set(row) != {"step", "status", "ms", "note"} or type(row["ms"]) not in (int, float)
                or row["ms"] < 0 or row["status"] != ("deferred" if row["step"] in DEFERRED_STEPS else "executed")
                or row["note"] is not None and not isinstance(row["note"], str)):
            _bad("paper_redline_display_metadata", stage="paper.step")
    emitted.raw["redline"] = deepcopy(trace)
    equal(inputs["paper.step"], {"state": before, "result": emitted, "run_date": day},
          "paper_original_account_and_complete_model_result")
    symbols = {row["ticker"] for row in before["positions"] + before["pending"]}
    symbols.update(row["ticker"] for row in before.get("entry_plans", {}).values()
                   if row["status"] in {"pending", "partial"})
    symbols.update(pick.ticker for pick in emitted.picks)
    symbols.update((emitted.raw.get("execution_setups") or {}).keys())
    sectors = {row.get("sector") for row in before["positions"]}
    sectors.update(row.get("sector") for row in before.get("entry_plans", {}).values())
    symbols.update(sector for sector in sectors if sector)
    groups = []
    provider_names = inputs["daily.preflight"]["policy"]["fields"]["provider_names"]
    for call in outputs["paper.step"]["io_calls"]:
        name, method, request, response, failed = _provider_call(run, "paper.step", call,
            provider_names, {"ohlcv", "available"})
        if method == "available":
            if not groups:
                _bad("paper_availability_without_observation", stage="paper.step")
            groups[-1][1].append((name, method, response, failed))
            continue
        symbol = request["args"][0]
        if not groups or groups[-1][0] != symbol:
            groups.append((symbol, []))
        groups[-1][1].append((name, method, response, failed))
    equal([symbol for symbol, _ in groups], sorted(symbols), "all_paper_observation_requests")
    bars = {}
    for symbol, calls in groups:
        selected = None
        available = set()
        for name, method, raw, failed in calls:
            if selected is not None:
                _bad("paper_IO_after_successful_observation", stage="paper.step")
            if method == "available":
                if not failed and raw is True:
                    available.add(name)
                continue
            primary = outputs["daily.preflight"]["value"]["provider"]
            if name != primary and name not in available and not (name == "stooq" and primary == "yahoo"):
                _bad("paper_fallback_without_available_probe", stage="paper.step")
            if failed:
                continue
            observed = market_observation(_restore(raw, native=True), run_date=day,
                                          market_timezone="America/New_York")
            if isinstance(observed, (pd.DataFrame, pd.Series)):
                observed = observed[observed.index.date <= cutoff]
            if not _missing_data(observed) and not _series_stale(observed, as_of_date=day):
                selected = observed
        bar = None
        # Match main.get_bar's explicit unavailable-data path. A successful
        # non-frame observation is not an executable bar or an invented mark.
        if isinstance(selected, pd.DataFrame) and len(selected):
            try:
                selected = selected[selected.index.date <= cutoff]
                if len(selected):
                    row = selected.iloc[-1]
                    adv = (float((selected["Close"] * selected["Volume"]).tail(20).mean())
                           if "Volume" in selected.columns else 0.)
                    bar = Bar(open=float(row["Open"]), high=float(row["High"]),
                              low=float(row["Low"]), close=float(row["Close"]),
                              adv=adv, date=str(selected.index[-1].date()))
            except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                # main.get_bar defers a malformed observation as unavailable;
                # it must not be upgraded to a fabricated complete OHLC bar.
                bar = None
        bars[symbol] = bar
    report = step_paper_account(before, day.isoformat(), emitted, bars)
    state = report["state"]
    transition = {key: value for key, value in report.items() if key != "state"}
    equal(outputs["paper.step"]["value"], {"transition": transition, "state": state, "bars": bars},
          "canonical_paper_fills_exits_friction_and_state")
    paper_path = {"__type__": "path", "value": str(run / "work/reports/sim_portfolio.json")}
    equal(inputs["paper.persist"], {"state": state, "path": paper_path}, "canonical_paper_persist_input", "paper.persist")
    calls = outputs["paper.persist"]["io_calls"]
    equal([call["method"] for call in calls], ["paper.save", "file.read_json"], "paper_persist_IO", "paper.persist")
    for call, request, response in zip(calls,
        ({"state": state, "path": paper_path}, {"path": paper_path["value"]}), (None, state)):
        if "error_ref" in call:
            _bad("paper_persist_failure", stage="paper.persist")
        equal(read_snapshot(run, call["input_ref"], run_id=run.name, stage="paper.persist", role="input"),
              request, "paper_persist_request", "paper.persist")
        equal(read_snapshot(run, call["output_ref"], run_id=run.name, stage="paper.persist", role="output"),
              response, "paper_persist_response", "paper.persist")
    engine = SimEngine.__new__(SimEngine)
    engine.state, engine.initial_cash, engine.path = state, state["initial_cash"], paper_path["value"]
    stats = engine.stats()
    accounting = {**policy, "status": "updated"}
    equal(outputs["paper.persist"]["value"], {"state": state, "stats": stats, "accounting": accounting},
          "canonical_paper_persisted_stats", "paper.persist")
    equal(inputs["daily.result"]["sim_state"], state, "final_canonical_paper_account", "daily.result")
    equal(inputs["daily.result"]["sim_transition"], transition, "final_canonical_paper_transition", "daily.result")
    equal(outputs["daily.result"]["value"]["paper"], state, "final_canonical_paper_readback", "daily.result")
