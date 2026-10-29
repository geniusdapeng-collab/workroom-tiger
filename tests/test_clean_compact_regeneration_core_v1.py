"""Synthetic transport/codec tests: no provider, account or business execution."""
from copy import deepcopy
from datetime import date
from hashlib import sha256
import io
import json
import random
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from trading_system.llm import clean_output as wire, required
from trading_system.llm.deepseek_required import (
    BASE_URL, MODEL, DeepSeekRequiredClient, _canonical, _hash, _json_loads, validate_schema,
)
from trading_system.llm.request_budget import BUDGET_POLICY_V3, token_upper_bound
from trading_system.llm.required_schemas import clean_schema_v2
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import ExecutionPolicy, UPGRADE_POLICY_ID, UPGRADE_MODEL_STAGES, RequiredModelFailed
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from trading_system.search.models import RawDocument

TEST_KEY = 'SYNTHETIC_MODEL_TEST_KEY'
ENVIRONMENT = {'LLM_BACKEND': 'api', 'LLM_BASE_URL': BASE_URL, 'LLM_MODEL': MODEL, 'LLM_API_KEY': TEST_KEY}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('NETWORK_FORBIDDEN_IN_SYNTHETIC_CORE_TEST')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(requests, 'post', deny)


def contract():
    ids = ['synthetic_source_' + str(index) for index in range(5)]
    chains, etfs = ['chain.one', 'chain:two', 'chain_three', 'chain-four'], ['SMH', 'BRK.B', 'A-B']
    parts = {ids[0]: ['p0', 'p2'], ids[1]: ['p1'], ids[2]: ['p0', 'p1'], ids[3]: []}
    schema = required.clean_keyed_schema_v2(clean_schema_v2(ids[:4], chains, etfs,
        quote_part_ids=parts, all_doc_ids=ids), ids)
    value = {'d': {}}
    for index in range(4):
        value['d'][f'd{index}'] = {'t': ['BRK.B', 'ABC-D', 'T0'], 's': 'mixed', 'v': -.125,
            'e': ['policy', 'earnings'], 'z': 'SYNTHETIC_TEST 中文😀, untouched natural text',
            'r': .375, 'h': list(reversed(chains)), 'f': list(reversed(etfs)),
            'p': {'k': 'unknown', 'o': 'd4' if index == 0 else None,
                  'n': 'SYNTHETIC_TEST source, uncertainty 😀', 't': 'unknown'},
            'c': [] if index == 3 else [{'s': f'SYNTHETIC_TEST fact {number}, 😀',
                'q': parts[ids[index]][0], 'k': f'synthetic.fact:{number}', 't': 'background'} for number in range(2)]}
    validate_schema(value, schema)
    return schema, value


class SyntheticExecution:
    """Captures inputs before invoke and safe error metadata like real call_io."""
    def __init__(self, *, output_policy=wire.CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1, run_id='synthetic-run-compact'):
        self.run_id, self.run_date = run_id, date(2026, 10, 27)
        self.admitted_at_utc = '2026-10-27T16:00:00+00:00'
        self.upgrade = self.model_required = True
        self.policy = ExecutionPolicy(UPGRADE_POLICY_ID, model_mode='enabled',
            search_source_names=('deepseek_search',), budget_policy_id=BUDGET_POLICY_V3,
            clean_output_policy_id=output_policy)
        self._active = SimpleNamespace(spec=SimpleNamespace(name='clean.llm_semantic'))
        self.records = []

    def stage(self, name):
        self._active.spec.name = name

    def call_io(self, method, inputs, invoke):
        row = {'method': method, 'input': deepcopy(inputs)}
        self.records.append(row)
        try:
            result = invoke()
        except BaseException as error:
            row['error'] = {'error_type': type(error).__name__}
            if type(error) is RequiredModelUnavailable:
                row['error'].update(diagnostic=error.diagnostic, accounting=error.accounting)
            raise
        row['output'] = deepcopy(result)
        return result


def make_client(*, output_policy=wire.CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1, run_id='synthetic-run-compact'):
    execution = SyntheticExecution(output_policy=output_policy, run_id=run_id)
    with patch.dict('os.environ', ENVIRONMENT):
        return DeepSeekRequiredClient(execution)


def tool_response(value, number=1, *, arguments=None, usage=None):
    return {'id': f'synthetic_response_{number}', 'model': MODEL, 'object': 'chat.completion',
        'system_fingerprint': 'synthetic_fingerprint',
        'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': f'synthetic_tool_{number}', 'type': 'function', 'function': {
                'name': wire.CLEAN_OUTPUT_FUNCTION, 'arguments': arguments if arguments is not None else json.dumps(value, ensure_ascii=False)}}]}}],
        'usage': usage if usage is not None else {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}}


def install_responses(client, responses):
    seen = []
    def post(path, request):
        assert len(client.execution.records) == len(seen) + 1  # input capture precedes paid invoke
        seen.append((path, deepcopy(request)))
        response = responses[len(seen) - 1]
        if isinstance(response, BaseException):
            raise response
        result = deepcopy(response)
        # Actual HTTP bytes can legally contain escaped unpaired Unicode even
        # when the decoded visible argument string cannot encode as UTF-8.
        return result, sha256(json.dumps(result, ensure_ascii=True).encode()).hexdigest()
    client._post_json = post
    return seen


def generate(client, schema, *, user='SYNTHETIC_TEST complete original input'):
    return client.complete_json(system='SYNTHETIC_TEST controlled system', user=user,
        schema_hint={}, schema=schema, max_tokens=32768, reasoning_effort='none')


def make_completed_summary_client():
    """Real production summary fixture: 1 rejection +6 accepted stages+14Search.

    It exercises transport/accounting metadata, not a full 43-stage business
    proof. The independent replay suite owns that separate full-chain fixture.
    """
    client = make_client(); schema, value = contract()
    good = wire.clean_compact_lists_output_v1(value, schema)
    bad = deepcopy(good); bad['d0__z'] = 'A' * 801
    number, clean_calls = 0, 0
    def post(path, request):
        nonlocal number, clean_calls
        number += 1
        if path == '/v1/tools/search':
            response = {'results': []}
        elif path == wire.CLEAN_OUTPUT_PATH:
            clean_calls += 1
            response = tool_response(bad if clean_calls == 1 else good, number)
        else:
            response = tool_response({}, number)
            response['choices'][0].update(finish_reason='stop', message={'role': 'assistant', 'content': '{"ok":"SYNTHETIC_TEST"}'})
        return response, _hash(response)
    client._post_json = post
    client.execution.stage('search.collect')
    for index in range(14): client.search('SYNTHETIC_TEST search ' + str(index))
    client.execution.stage('clean.llm_semantic'); generate(client, schema)
    simple = {'type': 'object', 'properties': {'ok': {'const': 'SYNTHETIC_TEST'}}, 'required': ['ok'], 'additionalProperties': False}
    for stage in UPGRADE_MODEL_STAGES[1:]:
        client.execution.stage(stage)
        client.complete_json(system='SYNTHETIC_TEST system', user='SYNTHETIC_TEST input', schema_hint={}, schema=simple)
    return client


@pytest.mark.parametrize('seed', range(20))
def test_full_original_roundtrip_preserves_order_types_unicode_and_nonlist_commas(seed):
    schema, value = contract(); rng = random.Random(seed)
    common = schema['properties']['d']['allOf'][0]['additionalProperties']['properties']
    for row in value['d'].values():
        row['t'] = rng.sample([f'T{index}' for index in range(8)], rng.randrange(9))
        for field in ('e', 'h', 'f'):
            values = common[field]['items']['enum']
            row[field] = rng.sample(values, rng.randrange(common[field]['maxItems'] + 1))
        row['v'] = rng.choice([0, 1, -1, -.125]); row['r'] = rng.choice([0, 1, .375])
    before = deepcopy((schema, value)); encoded = wire.clean_compact_lists_output_v1(value, schema)
    assert len(encoded) == 112 and wire.clean_compact_lists_output_v1(encoded, schema, decode=True) == value
    assert encoded['d0__z'] == value['d']['d0']['z']
    assert type(encoded['d0__v']) is type(value['d']['d0']['v']) and (schema, value) == before


@pytest.mark.parametrize('field', ('t', 'e', 'h', 'f'))
@pytest.mark.parametrize('bad', ('leading', 'trailing', 'empty_item', 'space', 'fullwidth', 'duplicate', 'overflow', 'list', 'null'))
def test_list_grammar_original_unique_capacity_and_types_reject_without_repair(field, bad):
    schema, value = contract(); encoded = wire.clean_compact_lists_output_v1(value, schema)
    first, second = value['d']['d0'][field][:2]
    maximum = schema['properties']['d']['allOf'][0]['additionalProperties']['properties'][field]['maxItems']
    encoded['d0__' + field] = {'leading': ',' + first, 'trailing': first + ',',
        'empty_item': first + ',,' + second, 'space': first + ', ' + second,
        'fullwidth': first + '，' + second, 'duplicate': first + ',' + first,
        'overflow': ','.join([first] * (maximum + 1)), 'list': [first], 'null': None}[bad]
    before = deepcopy(encoded)
    with pytest.raises(LLMUnavailable): wire.clean_compact_lists_output_v1(encoded, schema, decode=True)
    assert encoded == before


@pytest.mark.parametrize('mask', range(1, 15))
def test_partial_empty_claim_is_not_repaired_or_removed(mask):
    schema, value = contract(); encoded = wire.clean_compact_lists_output_v1(value, schema)
    for index, field in enumerate(('s', 'q', 'k', 't')):
        if mask & (1 << index): encoded[f'd0__c__s0__{field}'] = ''
    with pytest.raises(LLMUnavailable): wire.clean_compact_lists_output_v1(encoded, schema, decode=True)


@pytest.mark.parametrize('bad', ('hole', 'foreign_quote', 'unknown_source', 'extra_h_slot', 'missing_doc', 'bool_number', 'unknown_origin'))
def test_original_source_prefix_and_closed_scalar_gates_remain_mandatory(bad):
    schema, value = contract(); encoded = wire.clean_compact_lists_output_v1(value, schema)
    if bad == 'hole':
        for field in ('s', 'q', 'k', 't'): encoded['d0__c__s0__' + field] = ''
    elif bad == 'foreign_quote': encoded['d0__c__s0__q'] = 'p1'
    elif bad == 'unknown_source': encoded['d99__z'] = encoded.pop('d0__z')
    elif bad == 'extra_h_slot': encoded['d0__h__s6'] = ''
    elif bad == 'missing_doc': encoded = {key: value for key, value in encoded.items() if not key.startswith('d1__')}
    elif bad == 'bool_number': encoded['d0__v'] = True
    else: encoded['d0__p__o'] = 'd999'
    with pytest.raises(LLMUnavailable): wire.clean_compact_lists_output_v1(encoded, schema, decode=True)


@pytest.mark.parametrize('name,maximum', [('d0__z', 800), ('d0__p__n', 600), ('d0__c__s0__s', 600), ('d0__c__s0__k', 160)])
@pytest.mark.parametrize('letter', ['A', '中', '😀'])
def test_hard_original_text_bounds_keep_exact_unicode_lengths(name, maximum, letter):
    schema, value = contract(); encoded = wire.clean_compact_lists_output_v1(value, schema)
    if name.endswith('__k'): letter = 'a'
    projected = wire.expand_clean_compact_lists_provider_schema_v1(wire.clean_compact_lists_provider_schema_v1(schema), keyed_schema=schema)
    for length in (1, maximum - 1, maximum):
        encoded[name] = letter * length
        validate_schema(encoded, projected); assert wire.clean_compact_lists_output_v1(encoded, schema, decode=True)
    encoded[name] = letter * (maximum + 1)
    with pytest.raises(LLMUnavailable): validate_schema(encoded, projected)
    with pytest.raises(LLMUnavailable): wire.clean_compact_lists_output_v1(encoded, schema, decode=True)


@pytest.mark.parametrize('bad', ('remote', 'cycle', 'missing', 'unused', 'duplicate', 'extra_keyword', 'changed_bound', 'required_order'))
def test_provider_only_refs_have_exact_original_projection_and_canonical_interning(bad):
    schema, _ = contract(); provider = wire.clean_compact_lists_provider_schema_v1(schema)
    key = next(iter(provider['properties'])); alias = provider['properties'][key]['$ref'].split('/')[-1]
    if bad == 'remote': provider['properties'][key]['$ref'] = 'https://example.com/schema'
    elif bad == 'cycle': provider['$def'][alias] = {'$ref': '#/$def/' + alias}
    elif bad == 'missing': provider['$def'].pop(alias)
    elif bad == 'unused': provider['$def']['l' + str(len(provider['$def']))] = {'type': 'string'}
    elif bad == 'duplicate':
        new = 'l' + str(len(provider['$def'])); provider['$def'][new] = deepcopy(provider['$def'][alias]); provider['properties'][key]['$ref'] = '#/$def/' + new
    elif bad == 'extra_keyword': provider['properties'][key]['type'] = 'string'
    elif bad == 'changed_bound': provider['$def'][alias] = {'type': 'string', 'pattern': '.*'}
    else: provider['required'].reverse()
    with pytest.raises(wire.CleanOutputInvalid): wire.expand_clean_compact_lists_provider_schema_v1(provider, keyed_schema=schema)


def test_first_accepted_is_one_actual_attempt_and_preserves_compact_safe_arguments():
    client = make_client(); schema, value = contract(); encoded = wire.clean_compact_lists_output_v1(value, schema)
    seen = install_responses(client, [tool_response(encoded)])
    assert generate(client, schema) == encoded and len(seen) == len(client.receipts) == 1
    proof = client.execution.records[0]['output']
    assert proof['outcome'] == 'accepted' and proof['generation_index'] == 0 and proof['previous_rejection_sha256'] is None
    assert _json_loads(proof['response']['choices'][0]['message']['tool_calls'][0]['function']['arguments']) == encoded
    assert proof['parsed'] != value and 'visible_arguments_sha256' not in proof
    assert client.budget.snapshot()['actual_tokens'] == 110 and not client.budget.snapshot()['frozen']


def test_known_original_schema_rejection_then_whole_new_generation_is_two_sealed_charged_attempts():
    client = make_client(); schema, value = contract(); good = wire.clean_compact_lists_output_v1(value, schema)
    bad = deepcopy(good); bad['d0__z'] = 'A' * 801
    raw = json.dumps(bad, ensure_ascii=False, indent=2)
    seen = install_responses(client, [tool_response(bad, 1, arguments=raw), tool_response(good, 2)])
    assert generate(client, schema) == good and len(seen) == 2
    rejected, accepted = [row['output'] for row in client.execution.records]
    assert rejected['outcome'] == 'rejected' and 'parsed' not in rejected
    assert rejected['rejection_diagnostic']['code'] == 'OUTPUT_SCHEMA_MISMATCH'
    assert rejected['response']['choices'][0]['message']['tool_calls'][0]['function']['arguments'] == raw
    assert rejected['visible_arguments_sha256'] == sha256(raw.encode()).hexdigest()
    assert accepted['generation_index'] == 1 and accepted['previous_rejection_sha256'] == _hash(rejected)
    assert accepted['logical_call_sha256'] == rejected['logical_call_sha256']
    assert seen[0][1]['messages'][1] == seen[1][1]['messages'][1]
    assert seen[0][1]['tools'] == seen[1][1]['tools'] and 'A' * 100 not in seen[1][1]['messages'][0]['content']
    assert client.execution.records[1]['input']['previous_rejection_sha256'] == _hash(rejected)
    assert [row['outcome'] for row in client.receipts] == ['rejected', 'accepted']
    assert all('response' not in row and 'parsed' not in row and 'rejection_diagnostic' not in row for row in client.receipts)
    assert client.budget.snapshot()['actual_tokens'] == 220 and client._clean_regenerations_used == 1


def test_second_schema_rejection_retains_real_output_and_charge_without_extra_error_io():
    client = make_client(); schema, value = contract(); bad = wire.clean_compact_lists_output_v1(value, schema); bad['d0__z'] = 'A' * 801
    seen = install_responses(client, [tool_response(bad, 1), tool_response(bad, 2)])
    with pytest.raises(RequiredModelUnavailable): generate(client, schema)
    assert len(seen) == len(client.execution.records) == len(client.receipts) == 2
    assert all(row['output']['outcome'] == 'rejected' and 'error' not in row for row in client.execution.records)
    budget = client.budget.snapshot(); assert budget['actual_tokens'] == 220 and budget['reserved_tokens'] == 0 and not budget['frozen']


def test_four_distinct_batches_really_regenerate_then_fifth_rejection_is_retained_without_tenth_paid_call():
    client = make_client(); schema, value = contract(); good = wire.clean_compact_lists_output_v1(value, schema)
    bad = deepcopy(good); bad['d0__z'] = 'A' * 801
    responses = [tool_response(bad if index % 2 == 0 else good, index + 1) for index in range(8)] + [tool_response(bad, 9)]
    seen = install_responses(client, responses)
    for batch in range(4): assert generate(client, schema, user='SYNTHETIC_TEST complete batch ' + str(batch)) == good
    with pytest.raises(RequiredModelUnavailable): generate(client, schema, user='SYNTHETIC_TEST complete batch 4')
    assert len(seen) == 9 and client._clean_regenerations_used == 4
    assert len(client.receipts) == 9 and sum(row['outcome'] == 'rejected' for row in client.receipts) == 5
    assert client.execution.records[-1]['output']['generation_index'] == 0
    assert client.budget.snapshot()['actual_tokens'] == 990 and client.budget.snapshot()['reserved_tokens'] == 0


@pytest.mark.parametrize('bad', ['syntax', 'duplicate', 'nan', 'infinite', 'raw_secret', 'escaped_value_secret',
    'escaped_key_secret', 'escaped_unsafe_url', 'wrong_model', 'wrong_object', 'length', 'hidden_reasoning',
    'wrong_tool', 'unsafe_response_id', 'unsafe_tool_id', 'unsafe_fingerprint', 'raw_surrogate', 'decoded_surrogate',
    'missing_usage', 'inconsistent_usage', 'excess_usage', 'http', 'timeout'])
def test_only_safe_strict_json_original_schema_rejections_can_be_sealed_or_regenerated(bad):
    client = make_client(); schema, value = contract(); good = wire.clean_compact_lists_output_v1(value, schema)
    marker = 'SYNTHETIC_REJECTED_VISIBLE_MARKER'
    response = tool_response(good)
    function = response['choices'][0]['message']['tool_calls'][0]['function']
    escaped = ''.join('\\u' + format(ord(character), '04x') for character in TEST_KEY)
    if bad == 'syntax': function['arguments'] = '{"' + marker + '":'
    elif bad == 'duplicate': function['arguments'] = '{"' + marker + '":1,"' + marker + '":2}'
    elif bad == 'nan': function['arguments'] = '{"' + marker + '":NaN}'
    elif bad == 'infinite': function['arguments'] = '{"' + marker + '":1e999}'
    elif bad == 'raw_secret': function['arguments'] = '{"extra":"' + TEST_KEY + '"}'
    elif bad == 'escaped_value_secret': function['arguments'] = '{"extra":"' + escaped + '"}'
    elif bad == 'escaped_key_secret': function['arguments'] = '{"' + escaped + '":"' + marker + '"}'
    elif bad == 'escaped_unsafe_url': function['arguments'] = '{"extra":"https:\\/\\/example.com\\/?token=hidden"}'
    elif bad == 'wrong_model': response['model'] = 'SYNTHETIC_WRONG_MODEL'
    elif bad == 'wrong_object': response['object'] = 'SYNTHETIC_WRONG_OBJECT'
    elif bad == 'length': response['choices'][0]['finish_reason'] = 'length'
    elif bad == 'hidden_reasoning': response['choices'][0]['message']['reasoning_content'] = marker
    elif bad == 'wrong_tool': function['name'] = 'SYNTHETIC_WRONG_TOOL'
    elif bad == 'unsafe_response_id': response['id'] = 'api_key:synthetic-credential'
    elif bad == 'unsafe_tool_id': response['choices'][0]['message']['tool_calls'][0]['id'] = 'api_key:synthetic-credential'
    elif bad == 'unsafe_fingerprint': response['system_fingerprint'] = 'api_key:synthetic-credential'
    elif bad == 'raw_surrogate': function['arguments'] = '{"extra":"' + chr(0xD800) + '"}'
    elif bad == 'decoded_surrogate': function['arguments'] = '{"extra":"\\ud800"}'
    elif bad == 'missing_usage': response.pop('usage')
    elif bad == 'inconsistent_usage': response['usage']['total_tokens'] += 1
    elif bad == 'excess_usage': response['usage'] = {'prompt_tokens': 999999, 'completion_tokens': 1, 'total_tokens': 1000000}
    elif bad == 'http': response = RequiredModelUnavailable('HTTP_ERROR', http_status=400)
    else: response = RequiredModelUnavailable('NETWORK_TIMEOUT')
    seen = install_responses(client, [response])
    with pytest.raises(LLMUnavailable): generate(client, schema)
    assert len(seen) == 1 and client.receipts == [] and client._clean_regenerations_used == 0
    assert len(client.execution.records) == 1 and 'error' in client.execution.records[0]
    captured = _canonical(client.execution.records).decode()
    assert marker not in captured and escaped not in captured and TEST_KEY not in captured
    frozen = bad in {'missing_usage', 'inconsistent_usage', 'excess_usage', 'http', 'timeout'}
    assert client.budget.snapshot()['frozen'] is frozen
    assert client.budget.snapshot()['actual_tokens'] == (0 if frozen else 110)


@pytest.mark.parametrize('identity', ['response', 'tool'])
def test_rejected_identity_remains_reserved_globally_and_cannot_be_reused(identity):
    client = make_client(); schema, value = contract(); good = wire.clean_compact_lists_output_v1(value, schema)
    bad = deepcopy(good); bad['d0__z'] = 'A' * 801
    first, second = tool_response(bad, 1), tool_response(good, 2)
    if identity == 'response': second['id'] = first['id']
    else: second['choices'][0]['message']['tool_calls'][0]['id'] = first['choices'][0]['message']['tool_calls'][0]['id']
    seen = install_responses(client, [first, second])
    with pytest.raises(RequiredModelUnavailable): generate(client, schema)
    assert len(seen) == 2 and len(client.receipts) == 1
    assert client.execution.records[-1]['error']['diagnostic']['code'] == 'RESPONSE_IDENTITY_REUSED_OR_UNSAFE'
    assert client.budget.snapshot()['actual_tokens'] == 220 and not client.budget.snapshot()['frozen']


def test_unknown_second_attempt_keeps_first_charge_and_freezes_second_full_reservation():
    client = make_client(); schema, value = contract(); bad = wire.clean_compact_lists_output_v1(value, schema); bad['d0__z'] = 'A' * 801
    seen = install_responses(client, [tool_response(bad), RequiredModelUnavailable('NETWORK_TIMEOUT')])
    with pytest.raises(RequiredModelUnavailable): generate(client, schema)
    budget = client.budget.snapshot()
    assert len(seen) == 2 and budget['actual_tokens'] == 110 and budget['reserved_tokens'] > 32768 and budget['frozen']
    assert [row['status'] for row in budget['records']] == ['settled', 'unknown']


def test_nonblocking_reentry_and_same_logical_reconsumption_fail_before_another_paid_io():
    client = make_client(); schema, value = contract(); good = wire.clean_compact_lists_output_v1(value, schema)
    calls = []
    def post(path, request):
        calls.append(1)
        with pytest.raises(RequiredModelUnavailable): generate(client, schema)
        response = tool_response(good); return response, _hash(response)
    client._post_json = post
    assert generate(client, schema) == good and len(calls) == 1
    with pytest.raises(RequiredModelUnavailable): generate(client, schema)
    assert len(calls) == len(client.execution.records) == 1


def test_logical_identity_binds_run_and_full_admission_but_generation_user_input_stays_exact():
    schema, _ = contract(); request = wire.build_clean_compact_lists_request(system='SYNTHETIC_TEST', user='complete input', schema_hint={}, schema=schema)
    system = request['messages'][0]['content']
    assert 'q is a source quote-part token such as p3' in system
    assert 't is time_role and only accepts current, background or unknown' in system
    assert 'never copy q into t and never write a pN token into t' in system
    assessment = {'run_date': '2026-10-27', 'market_timezone': 'America/New_York', 'admitted_at_utc': '2026-10-27T16:00:00+00:00'}
    first = wire.clean_logical_call_sha256(run_id='synthetic_run_a', assessment=assessment, request=request, schema=schema)
    assert first != wire.clean_logical_call_sha256(run_id='synthetic_run_b', assessment=assessment, request=request, schema=schema)
    later = {**assessment, 'admitted_at_utc': '2026-10-27T16:01:00+00:00'}
    assert first != wire.clean_logical_call_sha256(run_id='synthetic_run_a', assessment=later, request=request, schema=schema)


def test_budget_deadline_before_regeneration_refuses_without_paid_io_or_usage_erasure():
    client = make_client(); schema, value = contract(); bad = wire.clean_compact_lists_output_v1(value, schema); bad['d0__z'] = 'A' * 801
    seen = install_responses(client, [tool_response(bad)])
    clock = client.budget._clock
    old_post = client._post_json
    def post(path, request):
        result = old_post(path, request)
        client.budget._clock = lambda: clock() + 2401
        return result
    client._post_json = post
    with pytest.raises(RequiredModelUnavailable): generate(client, schema)
    assert len(seen) == len(client.execution.records) == len(client.receipts) == 1
    assert client.budget.snapshot()['actual_tokens'] == 110 and not client.budget.snapshot()['frozen']
    assert client._clean_regenerations_used == 0


def test_exact_full_token_reservation_can_refuse_second_generation_after_first_is_settled():
    client = make_client(); schema, value = contract(); good = wire.clean_compact_lists_output_v1(value, schema)
    original = wire.build_clean_compact_lists_request(system='SYNTHETIC_TEST controlled system',
        user='SYNTHETIC_TEST complete original input', schema_hint={}, schema=schema)
    upper = sum(token_upper_bound(original, allow_clean_compact_regeneration=True))
    target = 600000 - upper - 50
    serial, remaining = 0, target
    client.execution.stage('research.candidates')
    simple = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}
    def normal(path, request):
        nonlocal serial
        serial += 1
        count = min(90000, remaining)
        response = tool_response({}, serial, usage={'prompt_tokens': count - 1, 'completion_tokens': 1, 'total_tokens': count})
        response['choices'][0].update(finish_reason='stop', message={'role': 'assistant', 'content': '{"ok":true}'})
        return response, _hash(response)
    client._post_json = normal
    while remaining:
        count = min(90000, remaining)
        client.complete_json(system='SYNTHETIC_TEST', user='X' * (count + 1000), schema_hint={}, schema=simple)
        remaining -= count
    assert client.budget.snapshot()['actual_tokens'] == target
    client.execution.stage('clean.llm_semantic')
    bad = deepcopy(good); bad['d0__z'] = 'A' * 801
    calls = []
    def rejecting(path, request):
        calls.append(1); response = tool_response(bad, serial + 1); return response, _hash(response)
    client._post_json = rejecting
    with pytest.raises(RequiredModelUnavailable): generate(client, schema)
    assert len(calls) == 1 and client.budget.snapshot()['actual_tokens'] == target + 110
    assert client.budget.snapshot()['reserved_tokens'] == 0 and not client.budget.snapshot()['frozen']


def test_old_explicit_strict_selector_keeps_single_attempt_terminal_schema_failure():
    client = make_client(output_policy=wire.CLEAN_OUTPUT_POLICY_V1); schema, _ = contract()
    seen = install_responses(client, [tool_response({'d': {}}, 1)])
    with pytest.raises(RequiredModelUnavailable): generate(client, schema)
    assert len(seen) == 1 and client.receipts == [] and 'error' in client.execution.records[0]
    assert client._clean_regenerations_used == 0 and client.budget.snapshot()['actual_tokens'] == 110


def test_zero_sources_require_a_real_new_call_and_accept_exact_empty_compact_root():
    schema = required.clean_keyed_schema_v2(clean_schema_v2([], ['chain'], ['SMH'], quote_part_ids={}, all_doc_ids=[]), [])
    assert wire.clean_compact_lists_output_v1({}, schema, decode=True) == {'d': {}}
    client = make_client(); seen = install_responses(client, [tool_response({})])
    assert generate(client, schema) == {} and len(seen) == 1
    assert client.execution.records[0]['output']['parsed'] == {} and len(client.receipts) == 1


@pytest.mark.parametrize('selector,size', [(wire.CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1, 4), (None, 8), (wire.CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1, 8)])
def test_new_only_four_batches_preserve_every_full_body_registry_assessment_and_quote_binding(selector, size):
    execution = SyntheticExecution(output_policy=selector)
    captures = []
    class Client:
        def __init__(self): self.execution = execution
        def complete_json(self, **arguments):
            task, serialized = arguments['user'].split('\n', 1); payload = json.loads(serialized)
            captures.append((arguments, payload))
            schema = arguments['schema']; rows = schema['properties']['d']['allOf'][1]['properties']
            value = {'d': {reference: {'t': [], 's': 'neutral', 'v': 0, 'e': [], 'z': 'SYNTHETIC_TEST summary',
                'r': 0, 'h': [], 'f': [], 'p': {'k': 'unknown', 'o': None, 'n': 'SYNTHETIC_TEST uncertainty', 't': 'unknown'}, 'c': []}
                for reference in rows}}
            if selector == wire.CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1: return wire.clean_compact_lists_output_v1(value, schema)
            if selector == wire.CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1: return required.clean_slots_output_v1(value, schema)
            return value
    docs = [RawDocument(f'synthetic_doc_{index}', 'deepseek_search', f'SYNTHETIC_TEST title{index}',
        'https://example.com/' + str(index), ('SYNTHETIC_TEST body 中文 ' + str(index) + ' ') * 60, '',
        1793116800, {}) for index in range(9)]
    result = required.clean_v2(Client(), docs)
    assert [row.raw for row in result] == docs
    assert [len(payload['documents']['rows']) for _, payload in captures] == ([4, 4, 1] if size == 4 else [8, 1])
    all_refs, bodies = [], []
    for arguments, payload in captures:
        assert len(payload['all_source_index']['rows']) == 9
        assert payload['assessment']['admitted_at_utc'] == execution.admitted_at_utc
        column = payload['documents']['columns'].index('source_ref'); parts = payload['documents']['columns'].index('content_parts')
        all_refs.extend(row[column] for row in payload['documents']['rows'])
        bodies.extend(''.join(part[1] for part in row[parts]['rows']) for row in payload['documents']['rows'])
        assert arguments['max_tokens'] == (32768 if size == 4 else 16384)
    assert all_refs == ['d' + str(index) for index in range(9)]
    from trading_system.llm.candidate_research import source_view
    assert bodies == [source_view(doc, characters=len(doc.content))['content'] for doc in docs]


def test_real_production_summary_counts_all_attempts_and_all_six_accepted_stages():
    client = make_completed_summary_client(); summary = required.model_summary_v2(client)
    assert summary['call_count'] == 7 and summary['accepted_call_count'] == 6 and summary['rejected_call_count'] == 1
    assert summary['stage_counts']['clean.llm_semantic'] == 2 and len(summary['response_ids']) == 7
    assert summary['budget']['counts'] == {'chat': 7, 'search': 14, 'fetch': 0}
    assert summary['regeneration_policy_id'] == wire.CLEAN_REGENERATION_POLICY_V1


@pytest.mark.parametrize('bad', ['outcome', 'predecessor', 'logical', 'generation', 'missing_attempt', 'duplicate_id', 'wrong_codec', 'legacy_smuggle'])
def test_summary_cannot_hide_charges_or_smuggle_new_metadata_into_old_protocol(bad):
    client = make_completed_summary_client(); rejected, accepted = client.receipts[:2]
    if bad == 'outcome': rejected['outcome'] = 'accepted'
    elif bad == 'predecessor': accepted['previous_rejection_sha256'] = 'f' * 64
    elif bad == 'logical': accepted['logical_call_sha256'] = 'f' * 64
    elif bad == 'generation': accepted['generation_index'] = 0
    elif bad == 'missing_attempt': client.receipts.pop(0)
    elif bad == 'duplicate_id': accepted['response_id'] = rejected['response_id']
    elif bad == 'wrong_codec': accepted['output_codec_id'] = wire.CLEAN_SLOTS_WIRE_V1 if hasattr(wire, 'CLEAN_SLOTS_WIRE_V1') else 'SYNTHETIC_WRONG'
    else:
        client.execution.policy = ExecutionPolicy(UPGRADE_POLICY_ID, model_mode='enabled', search_source_names=('deepseek_search',),
            budget_policy_id=BUDGET_POLICY_V3, clean_output_policy_id=wire.CLEAN_OUTPUT_POLICY_V1)
    with pytest.raises(RequiredModelFailed): required.model_summary_v2(client)


def test_new_actual_http_body_is_exact_prepaid_canonical_utf8_and_old_body_keeps_json_framing(monkeypatch):
    schema, value = contract(); parsed = wire.clean_compact_lists_output_v1(value, schema)
    request = wire.build_clean_compact_lists_request(system='SYNTHETIC_TEST', user='SYNTHETIC_TEST 中文', schema_hint={}, schema=schema)
    body = _canonical(tool_response(parsed)); seen = []
    class Response:
        status_code = 200
        headers = {'Content-Type': 'application/json', 'Content-Encoding': 'identity'}
        def __init__(self): self.raw = io.BytesIO(body)
        def close(self): self.raw.close()
    def post(url, **options): seen.append(options); return Response()
    monkeypatch.setattr(requests, 'post', post)
    client = make_client(); client._post_json(wire.CLEAN_OUTPUT_PATH, request)
    assert seen[0]['data'] == _canonical(request) and 'json' not in seen[0]
    upper = token_upper_bound(request, allow_clean_compact_regeneration=True)
    assert upper == (len(seen[0]['data']) + 4096 + 128, 32768)
    old = make_client(output_policy=wire.CLEAN_OUTPUT_POLICY_V1)
    old._post_json(wire.CLEAN_OUTPUT_PATH, request)
    assert seen[1]['json'] == request and 'data' not in seen[1]
