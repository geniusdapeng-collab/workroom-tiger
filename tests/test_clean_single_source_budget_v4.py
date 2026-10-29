"""Synthetic v4 main-flow boundaries; no network and no business success claim."""
from copy import deepcopy
from dataclasses import replace
import json
import socket
from unittest.mock import patch

import pytest
import requests

import test_clean_compact_regeneration_core_v1 as core
from trading_system.llm import clean_output as wire, required
from trading_system.llm.candidate_research import source_view
from trading_system.llm.deepseek_required import DeepSeekRequiredClient, _hash
from trading_system.llm.request_budget import BUDGET_POLICY_V3, BUDGET_POLICY_V4, semantic_output_tokens
from trading_system.redline.execution import UPGRADE_MODEL_STAGES
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from trading_system.search.models import RawDocument


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('NETWORK_FORBIDDEN_IN_SINGLE_SOURCE_TEST')
    for name in ('connect', 'connect_ex'):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(requests, 'post', deny)


def client_v4():
    execution = core.SyntheticExecution()
    execution.policy = replace(execution.policy, budget_policy_id=BUDGET_POLICY_V4)
    with patch.dict('os.environ', core.ENVIRONMENT):
        return DeepSeekRequiredClient(execution)


def documents(count):
    return [RawDocument(f'synthetic_source_{number}', 'deepseek_search',
        f'SYNTHETIC_TEST title {number}', f'https://example.com/{number}',
        (f'SYNTHETIC_TEST complete body {number} 中文😀. ' * 20),
        ('2026-10-27' if number % 3 == 0 else '2020-01-01' if number % 3 == 1 else ''),
        1793116800, {'topic': f'SYNTHETIC_TEST_topic_{number % 14}'}) for number in range(count)]


def install_transport(client, *, regenerate=False):
    seen, attempts = [], {}
    def post(path, request):
        number = len(seen) + 1
        seen.append((path, deepcopy(request)))
        if path == '/v1/tools/search':
            response = {'results': []}
        elif path == wire.CLEAN_OUTPUT_PATH:
            payload = json.loads(request['messages'][1]['content'].split('\n', 1)[1])
            refs = payload['clean_wire']['output_source_refs']
            identity = tuple(refs)
            attempts[identity] = attempts.get(identity, 0) + 1
            keyed = {ref: {'t': [], 's': 'neutral', 'v': 0, 'e': [],
                'z': 'SYNTHETIC_TEST full source interpretation', 'r': .5, 'h': [], 'f': [],
                'p': {'k': 'unknown', 'o': None, 'n': 'SYNTHETIC_TEST uncertainty retained', 't': 'unknown'},
                'c': []} for ref in refs}
            # Construct this test-only synthetic wire answer directly; production
            # still performs strict parsing, every schema, decoding and coverage.
            answer = {}
            for ref, row in keyed.items():
                for field in ('t', 's', 'v', 'e', 'z', 'r', 'h', 'f'):
                    answer[ref + '__' + field] = ','.join(row[field]) if isinstance(row[field], list) else row[field]
                answer.update({ref + '__p__' + key: value if value is not None else '' for key, value in row['p'].items()})
                answer.update({f'{ref}__c__s{index}__{field}': '' for index in range(4) for field in ('s', 'q', 'k', 't')})
            if regenerate and refs and attempts[identity] == 1:
                answer[refs[0] + '__z'] = 'X' * 801
            response = core.tool_response(answer, number)
        else:
            response = core.tool_response({}, number)
            response['choices'][0].update(finish_reason='stop', message={'role': 'assistant', 'content': '{"ok":true}'})
        return response, _hash(response)
    client._post_json = post
    return seen


@pytest.mark.parametrize('count', [0, 1, 55, 56])
def test_each_source_once_preserves_full_body_dates_registry_and_assessment(count):
    client = client_v4(); seen = install_transport(client); docs = documents(count)
    result = required.clean_v2(client, docs)
    assert [row.raw for row in result] == docs
    assert len(seen) == max(1, count)
    refs, bodies = [], []
    for _, request in seen:
        payload = json.loads(request['messages'][1]['content'].split('\n', 1)[1])
        table = payload['documents']; rows = table['rows']
        assert len(rows) == (1 if count else 0)
        assert payload['assessment']['admitted_at_utc'] == client.execution.admitted_at_utc
        assert payload['assessment']['run_date'] == client.execution.run_date.isoformat()
        registry = [dict(zip(payload['all_source_index']['columns'], row)) for row in payload['all_source_index']['rows']]
        assert [row['id'] for row in registry] == [doc.doc_id for doc in docs]
        assert [row['published_at'] for row in registry] == [doc.published or None for doc in docs]
        refs.extend(payload['clean_wire']['output_source_refs'])
        bodies.extend(''.join(part[1] for part in row[table['columns'].index('content_parts')]['rows']) for row in rows)
        assert len(request['tools'][0]['function']['parameters']['properties']) == (28 if count else 0)
    assert refs == [f'd{index}' for index in range(count)]
    assert bodies == [source_view(doc, characters=len(doc.content))['content'] for doc in docs]
    assert client.budget.snapshot()['reserved_tokens'] == 0


def test_all_56_sources_can_regenerate_and_summary_reconciles_every_attempt():
    client = client_v4(); seen = install_transport(client, regenerate=True)
    client.execution.stage('search.collect')
    for index in range(14): client.search('SYNTHETIC_TEST theme ' + str(index))
    client.execution.stage('clean.llm_semantic')
    assert len(required.clean_v2(client, documents(56))) == 56
    simple = {'type': 'object', 'properties': {'ok': {'const': True}}, 'required': ['ok'], 'additionalProperties': False}
    for stage in UPGRADE_MODEL_STAGES[1:]:
        client.execution.stage(stage)
        client.complete_json(system='SYNTHETIC_TEST', user='SYNTHETIC_TEST', schema_hint={}, schema=simple,
            max_tokens=semantic_output_tokens(stage, client.execution.policy.budget_policy_id))
    semantic = [request for path, request in seen if path == '/chat/completions']
    assert len(semantic) == 5
    assert all(request['max_tokens'] == 65536 and request['thinking'] == {'type': 'disabled'}
               and 'reasoning_effort' not in request for request in semantic)
    summary = required.model_summary_v2(client)
    assert summary['rejected_call_count'] == client._clean_regenerations_used == 56
    assert summary['accepted_call_count'] == 61 and summary['call_count'] == 117
    assert summary['budget']['counts'] == {'chat': 117, 'search': 14, 'fetch': 0}
    assert len(summary['response_ids']) == len(set(summary['response_ids'])) == 117
    assert summary['budget']['schema'] == 'tiger.request-budget.v4'
    assert summary['budget']['actual_tokens'] == 117 * 110 and not summary['budget']['frozen']
    assert summary['budget']['reserved_tokens'] == 0
    assert all(row['reservation']['schema'] == 'tiger.request-budget-reservation.v4' for row in summary['budget']['records'])
    clean = [request for path, request in seen if path == wire.CLEAN_OUTPUT_PATH]
    for first, second in zip(clean[::2], clean[1::2]):
        assert first['messages'][1] == second['messages'][1] and first['tools'] == second['tools']
        assert first['messages'][0] != second['messages'][0]


def test_v4_cannot_bypass_single_source_at_direct_model_entry():
    client = client_v4(); schema, value = core.contract()
    seen = core.install_responses(client, [core.tool_response(wire.clean_compact_lists_output_v1(value, schema))])
    with pytest.raises(RequiredModelUnavailable): core.generate(client, schema)
    assert seen == [] and client.budget.snapshot()['records'] == []


def test_legacy_selector_keeps_old_batch_and_regeneration_limits():
    execution = core.SyntheticExecution()
    assert execution.policy.budget_policy_id == BUDGET_POLICY_V3
    assert wire.clean_compact_execution_limits(execution) == (4, 4)
    assert wire.clean_compact_execution_limits(client_v4().execution) == (1, 56)
