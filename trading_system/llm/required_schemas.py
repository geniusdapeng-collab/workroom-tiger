"""Closed schemas for evidence-bound, model-required semantic stages.

Builders describe allowed output only. They do not infer, repair, clamp,
default or authorize a trading action. Cross-item coverage and action
authority are checked by the execution owner after schema validation.
"""
from __future__ import annotations

import re

PLAIN_TEXT_PATTERN = r"^(?!\s*$)[^<>\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]+$"
SYMBOL_PATTERN = r"^[A-Z][A-Z0-9.\-]{0,14}$"
TICKER_PATTERN = r"^[A-Z0-9][A-Z0-9.\-]{0,14}$"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_CHAIN_PATTERN = r"^[A-Za-z][A-Za-z0-9._:-]{0,63}$"
_EVENTS = ["earnings", "guidance", "downgrade", "upgrade", "lawsuit",
           "supply_chain", "policy", "patent", "capacity", "macro", "other"]


def _ids(values, *, allow_empty: bool = False, symbols: bool = False,
         chains: bool = False) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > 1000 or not values and not allow_empty:
        raise ValueError("MODEL_SCHEMA_ID_SET_REQUIRED")
    pattern = SYMBOL_PATTERN if symbols else _CHAIN_PATTERN if chains else _ID_PATTERN
    if any(not isinstance(value, str) or re.fullmatch(pattern, value) is None for value in values):
        raise ValueError("MODEL_SCHEMA_INVALID_ID")
    if len(set(values)) != len(values):
        raise ValueError("MODEL_SCHEMA_DUPLICATE_ID")
    return list(values)


def _object(**properties) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _text(maximum: int) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": maximum,
            "pattern": PLAIN_TEXT_PATTERN}


def _enum(values: list[str]) -> dict:
    return {"type": "string", "enum": list(values)}


def _number(low: float, high: float) -> dict:
    return {"type": "number", "minimum": low, "maximum": high}


def _array(items: dict, low: int, high: int, *, unique: bool = False) -> dict:
    return {"type": "array", "items": items, "minItems": low, "maxItems": high,
            "uniqueItems": unique}


def _evidence(doc_ids: list[str]) -> dict:
    return _array(_enum(doc_ids), 1, 8, unique=True)


def clean_schema(doc_ids) -> dict:
    docs = _ids(doc_ids)
    document = _object(
        id=_enum(docs),
        tickers=_array({"type": "string", "pattern": TICKER_PATTERN}, 0, 8, unique=True),
        sentiment=_enum(["bullish", "bearish", "neutral", "mixed"]),
        sentiment_score=_number(-1, 1),
        events=_array(_enum(_EVENTS), 0, 6, unique=True),
        summary_zh=_text(600), relevance=_number(0, 1),
    )
    return _object(documents=_array(document, len(docs), len(docs), unique=True))


def sentiment_schema(chain_ids, doc_ids) -> dict:
    chains, docs = _ids(chain_ids, chains=True), _ids(doc_ids)
    chain = _object(
        chain_id=_enum(chains), heat=_number(0, 10), sentiment_score=_number(-1, 1),
        narrative_change=_enum(["improving", "stable", "deteriorating"]),
        key_drivers=_array(_text(600), 1, 5, unique=True), evidence_ids=_evidence(docs),
    )
    return _object(chains=_array(chain, len(chains), len(chains), unique=True))


def risk_schema(chain_ids, doc_ids) -> dict:
    chains, docs = _ids(chain_ids, chains=True), _ids(doc_ids)
    allowed = chains if "unknown" in chains else [*chains, "unknown"]
    alert = _object(
        severity=_number(1, 10), chain_id=_enum(allowed),
        link=_enum(["upstream", "midstream", "downstream", "unknown"]),
        type=_enum(["supply_chain", "policy", "patent", "capacity", "other"]),
        headline_zh=_text(400), transmission=_array(_text(300), 1, 6, unique=True),
        evidence_ids=_evidence(docs),
    )
    return _object(assessment=_text(1000), alerts=_array(alert, 0, 20, unique=True))


def narrative_schema(etfs, doc_ids) -> dict:
    sectors, docs = _ids(etfs, symbols=True), _ids(doc_ids)
    sector = _object(
        etf=_enum(sectors), narrative_score=_number(0, 10),
        eps_revision_bias=_enum(["up", "flat", "down", "unknown"]),
        guidance_tone=_enum(["positive", "neutral", "negative", "unknown"]),
        evidence=_array(_text(600), 1, 3, unique=True), evidence_ids=_evidence(docs),
    )
    return _object(sectors=_array(sector, len(sectors), len(sectors), unique=True))


def decision_schema(symbols, doc_ids, market_ids) -> dict:
    candidates, docs = _ids(symbols, allow_empty=True, symbols=True), _ids(doc_ids)
    markets = _ids(market_ids, allow_empty=True)
    if (set(markets) & set(docs) or len(markets) != len(candidates)
            or any(re.fullmatch(r"md_[A-Z][A-Z0-9.\-]{0,14}_[0-9]{8}_[0-9a-f]{24}", item) is None
                   for item in markets)
            or any(sum(item.startswith("md_" + symbol + "_") for item in markets) != 1
                   for symbol in candidates)):
        raise ValueError("MODEL_SCHEMA_MARKET_EVIDENCE_REQUIRED")
    symbol = _enum(candidates) if candidates else {"type": "string", "pattern": r"(?!)"}
    decision = _object(
        symbol=symbol, action=_enum(["buy", "watch"]), action_summary=_text(300),
        why_this=_text(1200), why_now=_text(1200), entry_condition=_text(1200),
        invalidation=_text(1200), risk_note=_text(1200),
        source_ids=_array(_enum([*docs, *markets]), 1, 9, unique=True),
    )
    # A buy must carry at least its own market snapshot and one article. The
    # execution owner additionally verifies the exact citation types and owner.
    decision["allOf"] = [{"oneOf": [
        {"properties": {"action": {"const": "buy"}, "source_ids": {"minItems": 2}}},
        {"properties": {"action": {"const": "watch"}}},
    ]}]
    return _object(action=_enum(["BUY", "WAIT", "AVOID"]), action_summary=_text(1200),
                   decisions=_array(decision, 0, len(candidates), unique=True))


# The v1 builders above are deliberately immutable: archived parent receipts
# reconstruct those exact schemas. New research uses these separate builders.
def _text_v2(maximum: int) -> dict:
    # Python's $ also accepts the position before a final newline. Require the
    # actual end, using a negative any-character assertion portable to JS too.
    # The v2 format also excludes Unicode line/paragraph separators, word
    # joiners and the zero-width BOM.
    pattern = (PLAIN_TEXT_PATTERN[:-1].replace(r"\u202a-\u202e", r"\u2028-\u202e")
               .replace(r"\u2066-\u2069", r"\u2060-\u206f\ufeff"))
    return {**_text(maximum), "pattern": pattern + r"(?![\s\S])"}


def _allowed(values) -> dict:
    values = list(values)
    return _enum(values) if values else {"type": "string", "pattern": r"(?!)"}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _references(values, high=8, low=0) -> dict:
    return _array(_allowed(values), low, high, unique=True)


def clean_schema_v2(doc_ids, chain_ids, sector_symbols, *, quote_part_ids, all_doc_ids=None) -> dict:
    docs = _ids(doc_ids, allow_empty=True)
    origins = _ids(all_doc_ids if all_doc_ids is not None else docs, allow_empty=True)
    chains = _ids(chain_ids, chains=True)
    sectors = _ids(sector_symbols, symbols=True)
    if not isinstance(quote_part_ids, dict) or set(quote_part_ids) != set(docs):
        raise ValueError("MODEL_SCHEMA_SOURCE_QUOTE_PARTS_REQUIRED")
    parts = {identity: _ids(quote_part_ids[identity], allow_empty=True) for identity in docs}
    if any(re.fullmatch(r"p(?:0|[1-9][0-9]*)", part) is None
           for values in parts.values() for part in values):
        raise ValueError("MODEL_SCHEMA_INVALID_QUOTE_PART_ID")
    allowed_parts = list(dict.fromkeys(part for values in parts.values() for part in values))
    claim = _object(statement=_text_v2(600), quote_part_id=_allowed(allowed_parts),
                    claim_key={"type": "string", "minLength": 1, "maxLength": 160,
                               "pattern": r"^[a-z0-9][a-z0-9_.:-]{0,159}(?![\s\S])"},
                    time_role=_enum(["current", "background", "unknown"]))
    provenance = _object(
        evidence_kind=_enum(["primary", "media", "commentary", "unknown"]),
        origin_id=_nullable(_allowed(origins)), origin_note=_text_v2(600),
        time_role=_enum(["current", "background", "unknown"]),
    )
    document = _object(
        id=_allowed(docs),
        tickers=_array({"type": "string", "pattern": TICKER_PATTERN[:-1] + r"(?![\s\S])"}, 0, 8, unique=True),
        sentiment=_enum(["bullish", "bearish", "neutral", "mixed"]),
        sentiment_score=_number(-1, 1), events=_array(_enum(_EVENTS), 0, 6, unique=True),
        summary_zh=_text_v2(800), relevance=_number(0, 1),
        chain_ids=_references(chains, len(chains)),
        sector_etfs=_references(sectors, len(sectors)),
        provenance=provenance, claims=_array(claim, 0, 4, unique=True),
    )
    if docs:
        # One shared annotation schema plus a small identity-dependent binding.
        # Source-local p0/p1 IDs cannot select a part found only in a neighbor.
        # The supported validator's oneOf avoids unsupported if/then keywords.
        document["allOf"] = [{"oneOf": [{"properties": {
            "id": {"const": identity},
            "claims": {"items": {"properties": {
                "quote_part_id": _allowed(parts[identity])}}},
        }} for identity in docs]}]
    return _object(documents=_array(document, len(docs), len(docs), unique=True))


def sentiment_schema_v2(chain_ids, doc_ids) -> dict:
    chains, docs = _ids(chain_ids, chains=True), _ids(doc_ids, allow_empty=True)
    row = _object(
        chain_id=_enum(chains), heat=_nullable(_number(0, 10)),
        sentiment_score=_nullable(_number(-1, 1)),
        narrative_change=_enum(["improving", "stable", "deteriorating", "unknown"]),
        key_drivers=_array(_text_v2(800), 1, 5, unique=True),
        evidence_ids=_references(docs),
    )
    return _object(chains=_array(row, len(chains), len(chains), unique=True))


def risk_schema_v2(chain_ids, doc_ids) -> dict:
    chains, docs = _ids(chain_ids, chains=True), _ids(doc_ids, allow_empty=True)
    allowed = chains if "unknown" in chains else [*chains, "unknown"]
    alert = _object(
        severity=_number(1, 10), chain_id=_enum(allowed),
        link=_enum(["upstream", "midstream", "downstream", "unknown"]),
        type=_enum(["supply_chain", "policy", "patent", "capacity", "other"]),
        headline_zh=_text_v2(500), transmission=_array(_text_v2(500), 1, 6, unique=True),
        evidence_ids=_references(docs, low=1),
    )
    return _object(assessment=_text_v2(2400), evidence_ids=_references(docs),
                   alerts=_array(alert, 0, 20, unique=True))


def narrative_schema_v2(etfs, doc_ids) -> dict:
    sectors, docs = _ids(etfs, symbols=True), _ids(doc_ids, allow_empty=True)
    row = _object(
        etf=_enum(sectors), narrative_score=_nullable(_number(0, 10)),
        eps_revision_bias=_enum(["up", "flat", "down", "unknown"]),
        guidance_tone=_enum(["positive", "neutral", "negative", "unknown"]),
        evidence=_array(_text_v2(1000), 1, 5, unique=True), evidence_ids=_references(docs),
    )
    return _object(sectors=_array(row, len(sectors), len(sectors), unique=True))


def thesis_schema_v2(symbols, doc_ids, fact_ids, market_ids) -> dict:
    candidates = _ids(symbols, allow_empty=True, symbols=True)
    docs = _ids(doc_ids, allow_empty=True)
    facts = _ids(fact_ids, allow_empty=True)
    markets = _ids(market_ids, allow_empty=True)
    paragraph = _object(text=_text_v2(1000),
                        evidence_ids=_references([*docs, *facts, *markets], high=12, low=1))
    claim = _object(
        claim_id={"type": "string", "pattern": _ID_PATTERN[:-1] + r"(?![\s\S])"}, statement=_text_v2(700),
        source_id=_allowed(docs), quote=_text_v2(1000),
        origin_id=_nullable(_allowed(docs)),
        evidence_kind=_enum(["primary", "media", "commentary", "unknown"]),
        time_role=_enum(["current", "background", "unknown"]),
    )
    # A Fetch page has no observed publication date. An optional reference may
    # explicitly bind its date to an observed Search result for that same URL.
    claim["properties"]["date_source_id"] = _nullable(_allowed(docs))
    return _object(
        schema_version={"const": 2}, symbol=_allowed(candidates),
        strategy=_enum(["technical_flow_v2", "news_event_v2"]),
        status=_enum(["sufficient", "insufficient", "contradictory"]),
        issuer_name=_nullable(_text_v2(200)), issuer_identity_evidence_ids=_references(docs),
        five_part=_object(**{scope: paragraph for scope in ("market", "sector", "chain", "stock", "plan")}),
        claims=_array(claim, 0, 8, unique=True),
        event_risk=_object(status=_enum(["checked", "unknown"]), text=_text_v2(1000),
                           source_ids=_references(docs)),
        entry_triggers=_array(_text_v2(600), 1, 5, unique=True),
        falsification=_array(_text_v2(600), 1, 5, unique=True),
        risk_notes=_array(_text_v2(800), 1, 5, unique=True),
        gaps=_array(_text_v2(600), 0, 8, unique=True),
        followup=_object(needed={"type": "boolean"}, question=_nullable(_text_v2(40)),
                         fetch_source_id=_nullable(_allowed(docs))),
    )


def research_schema_v2(symbols, doc_ids, fact_ids, market_ids) -> dict:
    candidates = _ids(symbols, allow_empty=True, symbols=True)
    return _object(
        coverage_summary=_text_v2(1600),
        theses=_array(thesis_schema_v2(candidates, doc_ids, fact_ids, market_ids),
                      len(candidates), len(candidates), unique=True),
    )


def decision_schema_v2(symbols, doc_ids, market_ids, fact_ids=()) -> dict:
    candidates = _ids(symbols, allow_empty=True, symbols=True)
    docs, markets = _ids(doc_ids, allow_empty=True), _ids(market_ids, allow_empty=True)
    facts = _ids(fact_ids, allow_empty=True)
    if len(markets) != len(candidates) or len(set([*docs, *markets, *facts])) != len(docs) + len(markets) + len(facts):
        raise ValueError("MODEL_SCHEMA_MARKET_EVIDENCE_REQUIRED")
    row = _object(
        symbol=_allowed(candidates), action=_enum(["buy", "watch"]), action_summary=_text_v2(500),
        why_this=_text_v2(1600), why_now=_text_v2(1600), entry_condition=_text_v2(1600),
        invalidation=_text_v2(1600), risk_note=_text_v2(1600),
        source_ids=_references([*docs, *markets], high=9, low=1),
        thesis=thesis_schema_v2(candidates, docs, facts, markets),
    )
    return _object(action=_enum(["BUY", "WAIT", "AVOID"]), action_summary=_text_v2(2400),
                   decisions=_array(row, len(candidates), len(candidates), unique=True))


def _binding_rows_v4(bindings, symbols, docs, facts, markets):
    """Accept only a complete deterministic guide for this exact request."""
    try:
        if (type(bindings) is not dict or bindings["schema"] != "tiger.thesis-request-bindings.v1"
                or bindings["phase"] not in {"initial", "final", "decision"}
                or type(bindings["sources"]) is not list or type(bindings["targets"]) is not list):
            raise ValueError
        sources = bindings["sources"]
        targets = bindings["targets"]
        if (_ids([row["source_id"] for row in sources], allow_empty=True) != docs
                or _ids([row["symbol"] for row in targets], allow_empty=True, symbols=True) != symbols):
            raise ValueError
        for row in sources:
            options = row["date_options"]
            if type(options) is not list or not options or options[0]["date_source_id"] is not None:
                raise ValueError
            dates = [option["date_source_id"] for option in options]
            if len(set(dates)) != len(dates) or any(item is not None and item not in docs for item in dates):
                raise ValueError
            for option in options:
                if option["allowed_time_roles"] not in (["background", "unknown"], ["current", "background", "unknown"]):
                    raise ValueError
        for target in targets:
            if [row["scope"] for row in target["five_part"]] != ["market", "sector", "chain", "stock", "plan"]:
                raise ValueError
            for part in target["five_part"]:
                values = _ids(part["evidence_ids"])
                allowed = markets if part["scope"] == "stock" else facts
                if part["requirement"] != "at_least_one" or any(value not in allowed for value in values):
                    raise ValueError
                if part["scope"] == "stock" and (len(values) != 1 or not values[0].startswith("md_" + target["symbol"] + "_")):
                    raise ValueError
            if any(identity not in docs for identity in _ids(target["issuer_identity_evidence_ids"], allow_empty=True)):
                raise ValueError
            followup = target["followup"]
            if (type(followup["question_max_characters"]) is not int
                    or not 0 <= followup["question_max_characters"] <= 40
                    or any(identity not in docs for identity in _ids(followup["fetch_source_id"], allow_empty=True))):
                raise ValueError
            for option in target["news_current_complete_options"]:
                if option["source_id"] not in target["issuer_identity_evidence_ids"] or not any(
                        row["source_id"] == option["source_id"] and any(
                            date["date_source_id"] == option["date_source_id"] and "current" in date["allowed_time_roles"]
                            for date in row["date_options"]) for row in sources):
                    raise ValueError
        return sources, targets
    except (KeyError, TypeError, ValueError):
        raise ValueError("MODEL_SCHEMA_REQUEST_BINDINGS_REQUIRED") from None


def _date_binding_v4(source_id, date_source_id, **properties):
    """Null permits omission, matching claim.get(date_source_id) exactly."""
    result = {"properties": {"source_id": {"const": source_id},
                             "date_source_id": {"const": date_source_id}, **properties}}
    if date_source_id is not None:
        result["required"] = ["date_source_id"]
    return result


def _followup_binding_v4(target, phase):
    inactive = {"properties": {"needed": {"const": False}, "question": {"type": "null"},
                               "fetch_source_id": {"type": "null"}}}
    if phase != "initial":
        return inactive
    action = target["followup"]
    limit = action["question_max_characters"]
    question = ({**_text_v2(limit), "allOf": [{"pattern": r'^[^<>:"\x00-\x1f]*(?![\s\S])'}]}
                if limit else {"type": "null"})
    active = {"properties": {"needed": {"const": True},
                              "question": _nullable(question),
                              "fetch_source_id": _nullable(_allowed(action["fetch_source_id"]))},
              "anyOf": [{"properties": {"question": {"type": "string"}}},
                        {"properties": {"fetch_source_id": {"type": "string"}}}]}
    return {"oneOf": [inactive, active]}


def _news_binding_v4(target):
    choices = [{"properties": {"strategy": {"const": "technical_flow_v2"}}},
               {"properties": {"strategy": {"const": "news_event_v2"},
                               "status": _enum(["insufficient", "contradictory"])}}]
    sufficient = []
    for option in target["news_current_complete_options"]:
        identity = option["source_id"]
        match = _date_binding_v4(identity, option["date_source_id"],
            time_role={"const": "current"}, evidence_kind={"const": "primary"}, origin_id={"const": identity})
        sufficient.append({"properties": {
            "issuer_identity_evidence_ids": {"contains": {"const": identity}},
            "claims": {"contains": match}}})
    if sufficient:
        choices.append({"properties": {"strategy": {"const": "news_event_v2"},
            "status": {"const": "sufficient"}, "issuer_name": {"type": "string"}}, "anyOf": sufficient})
    return {"oneOf": choices}


def thesis_schema_v4(symbols, doc_ids, fact_ids, market_ids, *, bindings):
    """Add only existing semantic relations; leave archived v2 bytes intact."""
    from copy import deepcopy
    candidates = _ids(symbols, allow_empty=True, symbols=True)
    docs, facts, markets = (_ids(values, allow_empty=True) for values in (doc_ids, fact_ids, market_ids))
    sources, targets = _binding_rows_v4(bindings, candidates, docs, facts, markets)
    result = deepcopy(thesis_schema_v2(candidates, docs, facts, markets))
    claim = result["properties"]["claims"]["items"]
    if sources:
        claim["allOf"] = [{"oneOf": [
            _date_binding_v4(row["source_id"], option["date_source_id"],
                             time_role=_enum(option["allowed_time_roles"]))
            for row in sources for option in row["date_options"]]}]
    result["allOf"] = [{"oneOf": [
        {"properties": {"status": {"const": "sufficient"}}},
        {"properties": {"status": _enum(["insufficient", "contradictory"]), "gaps": {"minItems": 1}}}]}]
    if targets:
        choices = []
        for target in targets:
            parts = {part["scope"]: {"properties": {
                "evidence_ids": {"contains": _allowed(part["evidence_ids"])}}}
                for part in target["five_part"]}
            choices.append({"properties": {
                "symbol": {"const": target["symbol"]},
                "issuer_identity_evidence_ids": {"items": _allowed(target["issuer_identity_evidence_ids"])},
                "five_part": {"properties": parts},
                "followup": _followup_binding_v4(target, bindings["phase"])},
                "allOf": [_news_binding_v4(target)]})
        result["allOf"].append({"oneOf": choices})
    return result


def research_schema_v4(symbols, doc_ids, fact_ids, market_ids, *, bindings):
    candidates = _ids(symbols, allow_empty=True, symbols=True)
    return _object(coverage_summary=_text_v2(1600),
        theses=_array(thesis_schema_v4(candidates, doc_ids, fact_ids, market_ids, bindings=bindings),
                      len(candidates), len(candidates), unique=True))


def decision_schema_v4(symbols, doc_ids, market_ids, fact_ids=(), *, bindings, base_schema=None):
    from copy import deepcopy
    candidates = _ids(symbols, allow_empty=True, symbols=True)
    # The caller may already have applied another source-owned decision
    # contract. Add thesis bindings without discarding its fields or allOf.
    result = deepcopy(base_schema) if base_schema is not None else decision_schema_v2(candidates, doc_ids, market_ids, fact_ids)
    row = result["properties"]["decisions"]["items"]
    row["properties"]["thesis"] = thesis_schema_v4(candidates, doc_ids, fact_ids, market_ids, bindings=bindings)
    if candidates:
        choices = []
        for target in bindings["targets"]:
            market_id = next(part["evidence_ids"][0] for part in target["five_part"] if part["scope"] == "stock")
            choices.append({"properties": {
                "symbol": {"const": target["symbol"]},
                "thesis": {"properties": {"symbol": {"const": target["symbol"]}}},
                "source_ids": {"items": _allowed([*doc_ids, market_id]), "contains": {"const": market_id}}}})
        row.setdefault("allOf", []).append({"oneOf": choices})
    return result
