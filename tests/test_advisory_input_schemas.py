"""Published advisory examples and schemas agree on the executable input shape.

Cross-record science/identity checks and JSON token types remain runtime checks;
this suite explicitly demonstrates those boundaries instead of promising that a
JSON Schema validator proves the analysis safe or scientifically valid.
"""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from raven import checkpoint_advisor as C, forecast_scenarios as F, slurm_simulator as S

ROOT = Path(__file__).resolve().parents[1]
KINDS = {'checkpoint-advice': C.advise, 'forecast-scenarios': F.analyze, 'queue-model': S.simulate}


@pytest.fixture(scope='module')
def validators():
    documents = {name: json.loads((ROOT / 'docs/schemas' / (name + '.v1.schema.json')).read_text()) for name in KINDS}
    registry = Registry().with_resources((value['$id'], Resource.from_contents(value)) for value in documents.values())
    for value in documents.values():
        Draft202012Validator.check_schema(value)
    return {name: Draft202012Validator(value, registry=registry) for name, value in documents.items()}


def example(name):
    return json.loads((ROOT / 'examples' / (name + '.json')).read_text())


def assign(document, path, value):
    for key in path[:-1]:
        document = document[key]
    document[path[-1]] = value


@pytest.mark.parametrize('name', KINDS)
def test_published_example_validates_and_executes_without_input_mutation(validators, name):
    value = example(name)
    original = copy.deepcopy(value)
    validators[name].validate(value)
    result = KINDS[name](value)
    assert result['schema'] == 'raven.' + name + '-result/v1'
    assert value == original


@pytest.mark.parametrize('name,path', [
    ('checkpoint-advice', ()), ('checkpoint-advice', ('scope',)),
    ('checkpoint-advice', ('support',)), ('checkpoint-advice', ('checkpoint_seconds',)),
    ('checkpoint-advice', ('failure_model',)), ('forecast-scenarios', ()),
    ('forecast-scenarios', ('request',)), ('forecast-scenarios', ('context',)),
    ('forecast-scenarios', ('alternatives', 0)), ('queue-model', ()),
    ('queue-model', ('policy',)), ('queue-model', ('nodes', 0)),
    ('queue-model', ('jobs', 0)), ('queue-model', ('policy', 'accounts', 'lab')),
    ('queue-model', ('policy', 'qos', 'normal')),
])
def test_unknown_operational_fields_are_rejected_by_schema_and_runtime(validators, name, path):
    value = example(name)
    assign(value, path + ('execute_shell',), 'unapproved')
    assert not validators[name].is_valid(value)
    with pytest.raises(ValueError, match='unknown fields'):
        KINDS[name](value)


@pytest.mark.parametrize('name,path,bad', [
    ('checkpoint-advice', ('scope', 'nodes'), True),
    ('checkpoint-advice', ('scope', 'nodes'), 1.5),
    ('checkpoint-advice', ('support', 'compatible'), 1),
    ('checkpoint-advice', ('useful_seconds',), 0),
    ('checkpoint-advice', ('checkpoint_seconds', 'lower_seconds'), 1e-10),
    ('checkpoint-advice', ('failure_model', 'allocation_mtbf', 'upper_seconds'), 1e9 + 1),
    ('checkpoint-advice', ('preemption_notice_seconds',), -1),
    ('forecast-scenarios', ('request', 'cpus'), True),
    ('forecast-scenarios', ('request', 'cpus'), 1.5),
    ('forecast-scenarios', ('context', 'reservations_visible'), 1),
    ('forecast-scenarios', ('coverage',), 1),
    ('forecast-scenarios', ('now',), 1e12 + 1),
    ('forecast-scenarios', ('maximum_overrun_probability',), 0),
    ('forecast-scenarios', ('alternatives',), [{}] * 9),
    ('forecast-scenarios', ('observations',), [{}] * 1001),
    ('queue-model', ('seed',), True),
    ('queue-model', ('nodes', 0, 'cpus'), 1.5),
    ('queue-model', ('nodes', 0, 'cpus'), 100001),
    ('queue-model', ('policy', 'backfill'), 1),
    ('queue-model', ('policy', 'usage_half_life_seconds'), 0),
    ('queue-model', ('policy', 'qos', 'normal', 'max_cpus'), 10000001),
    ('queue-model', ('jobs', 0, 'duration'), 1e-7),
    ('queue-model', ('nodes',), []),
    ('queue-model', ('traces',), [{}] * 21),
])
def test_structural_type_and_bound_violations_fail_both(validators, name, path, bad):
    value = example(name)
    assign(value, path, bad)
    assert not validators[name].is_valid(value)
    with pytest.raises(ValueError):
        KINDS[name](value)


def test_forecast_queue_model_reference_and_required_origin_are_local(validators):
    value = example('forecast-scenarios')
    value['queue_model'] = example('queue-model')
    value['target_job_id'] = 'prepare'
    value['queue_model_issued_at'] = 0
    validators['forecast-scenarios'].validate(value)
    F.analyze(value)
    del value['queue_model_issued_at']
    assert not validators['forecast-scenarios'].is_valid(value)
    with pytest.raises(ValueError, match='issued_at'):
        F.analyze(value)


def test_optional_measured_duration_and_censored_history_shape(validators):
    value = example('checkpoint-advice')
    value['checkpoint_seconds'] = {'samples_seconds': [1, 2, 4], 'source': 'measured elapsed time'}
    value['failure_model'] = {'stationary': True, 'representative': True, 'observations': [
        {'id': 'attempt-1', 'exposure_seconds': 10000, 'outcome': 'timeout'}]}
    validators['checkpoint-advice'].validate(value)
    assert C.advise(value)['status'] == 'abstain'
    value['failure_model']['allocation_mtbf'] = value['checkpoint_seconds']
    assert not validators['checkpoint-advice'].is_valid(value)
    with pytest.raises(ValueError, match='estimated range'):
        C.advise(value)


@pytest.mark.parametrize('name,path', [('checkpoint-advice', ('scope', 'nodes')), ('queue-model', ('nodes', 0, 'cpus'))])
def test_mathematical_json_schema_integer_does_not_replace_runtime_token_check(validators, name, path):
    value = example(name)
    assign(value, path, 1.0)
    validators[name].validate(value)
    with pytest.raises(ValueError, match='integer'):
        KINDS[name](value)


def test_forecast_existing_numeric_normalization_is_preserved(validators):
    value = example('forecast-scenarios')
    value['request'].update(cpus=1.0, account=17)
    value['alternatives'][0]['request'].update(cpus=1.0, account=17)
    validators['forecast-scenarios'].validate(value)
    assert F.analyze(value)['status'] == 'abstain'


def test_descriptive_extensions_are_not_unknown_action_fields(validators):
    for name, run in KINDS.items():
        value = example(name)
        value['metadata'] = {'laboratory_note': {'text': 'descriptive extension', 'revision': 2}}
        validators[name].validate(value)
        run(value)


@pytest.mark.parametrize('name,change,error', [
    ('checkpoint-advice', lambda x: x['checkpoint_seconds'].update(lower_seconds=5), 'reversed'),
    ('forecast-scenarios', lambda x: x['alternatives'].append(copy.deepcopy(x['alternatives'][0])), 'unique'),
    ('queue-model', lambda x: x['jobs'][0]['depends'].append('fit-a'), 'cycle'),
    ('queue-model', lambda x: x['jobs'][0].update(account='not-declared'), 'undeclared'),
])
def test_runtime_only_cross_record_semantics_are_not_faked_in_schema(validators, name, change, error):
    value = example(name)
    change(value)
    validators[name].validate(value)
    with pytest.raises(ValueError, match=error):
        KINDS[name](value)


def test_complete_trace_and_reservation_contract(validators):
    value = example('queue-model')
    value['reservations'] = [{'id': 'window', 'start': 1, 'end': 20, 'nodes': ['node1'], 'accounts': ['lab']}]
    value['traces'] = []
    for role, offset in [('calibration', 0), ('validation', 10000)]:
        jobs = copy.deepcopy(value['jobs'])
        for row in jobs:
            row.update(submit=offset, actual_start=offset + 1, actual_end=offset + 1 + row['duration'])
        value['traces'].append({'id': role, 'role': role, 'origin': 'synthetic', 'jobs': jobs})
    validators['queue-model'].validate(value)
    assert S.simulate(value)['parameters']['calibration_samples'] == 3
    del value['traces'][0]['jobs'][0]['actual_start']
    assert not validators['queue-model'].is_valid(value)
    with pytest.raises(ValueError, match='both actual_start and actual_end'):
        S.simulate(value)


def test_forecast_outcome_order_is_a_runtime_only_invariant(validators):
    value = example('forecast-scenarios')
    value['observations'] = [{'id': 'attempt-1', 'request': copy.deepcopy(value['request']),
        'context': copy.deepcopy(value['context']), 'origin': 'recorded', 'state': 'completed',
        'submit': 1, 'issued_at': 2, 'actual_start': 3, 'actual_end': 4}]
    validators['forecast-scenarios'].validate(value)
    F.analyze(value)
    value['observations'][0]['actual_start'] = 1
    validators['forecast-scenarios'].validate(value)
    with pytest.raises(ValueError, match='precede actual starts'):
        F.analyze(value)
