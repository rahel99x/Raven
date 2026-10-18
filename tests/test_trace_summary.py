import copy
import json
import math
import random
import subprocess
import sys

import pytest

from raven import trace_summary as summary
from raven.trace_protocol import read_trace
from raven.trace_recorder import Recorder


def fixture(tmp_path, values, phase='train', rank=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(tmp_path, background_flush=False, job_id='5', attempt='one', initial_mono_ns=0)
    position = 1
    try:
        for index, value in enumerate(values):
            data = dict(span_id=str(index), name=phase)
            if rank is not None:
                data['rank'] = rank
            recorder.emit('span_start', data, thread='main', mono_ns=position)
            position += value
            recorder.emit('span_end', {'span_id': str(index)}, thread='main', mono_ns=position)
            position += 1
    finally:
        # The fixture's synthetic monotonic clock is also used for clean close.
        # Full bytes/digests are written and verified; no snapshot is rewritten.
        original = recorder.emit
        recorder.emit = lambda kind, data=None, **kw: original(kind, data, mono_ns=position, **kw)
        recorder.close()
    snapshot = read_trace(recorder.path)
    assert snapshot['status'] == 'COMPLETE'
    return recorder.path, snapshot



@pytest.mark.parametrize('values', [list(range(128)), [0] * 100, [1] * 99 + [1 << 50],
                                  [1 << n for n in range(60)], [1, 1, 1, 2, 2, 10**12]])
def test_exact_contracts_and_quantile_bounds_against_full_intervals(tmp_path, values):
    _, snapshot = fixture(tmp_path, values)
    result = summary.summarize(snapshot)
    summary.validate(result)
    for name, expected in [('count', len(values)), ('sum', sum(values)), ('min', min(values)), ('max', max(values))]:
        answer = summary.answer(result, name)
        assert answer['answerability'] == 'exact' and answer['value'] == expected
    assert summary.answer(result, 'top-k', k=5)['value'] == sorted(values, reverse=True)[:5]
    for q in [0, .1, .5, .95, .99, 1]:
        answer = summary.answer(result, 'quantile', quantile=q)
        exact = sorted(values)[max(0, math.ceil(q * len(values)) - 1)]
        assert answer['lower'] <= exact <= answer['upper']
        assert answer['upper'] - answer['lower'] <= max(0, exact / 64)


def test_merge_delete_and_query_contract_preservation(tmp_path):
    _, a = fixture(tmp_path / 'a', [1, 2, 1000], rank=0)
    _, b = fixture(tmp_path / 'b', [2, 3, 2000], rank=1)
    first, second = summary.summarize(a), summary.summarize(b)
    merged = summary.merge([first, second])
    assert summary.answer(merged, 'count')['value'] == 6
    assert summary.answer(merged, 'sum', phase='train', rank=1)['value'] == 2005
    assert summary.answer(merged, 'top-k', k=3)['value'] == [2000, 1000, 3]
    for q in [.5, .95]:
        answer = summary.answer(merged, 'quantile', quantile=q)
        exact = sorted([1, 2, 1000, 2, 3, 2000])[math.ceil(q * 6) - 1]
        assert answer['lower'] <= exact <= answer['upper']
    deleted = summary.delete(merged, first['sources'][0]['digest'])
    assert summary.answer(deleted, 'count')['value'] == 3
    with pytest.raises(ValueError, match='double count'):
        summary.merge([first, first])
    with pytest.raises(ValueError, match='absent'):
        summary.delete(merged, 'f' * 64)


def test_partial_windows_clock_domains_and_causality_abstain(tmp_path):
    _, snapshot = fixture(tmp_path, [10, 20])
    result = summary.summarize(snapshot)
    assert summary.answer(result, 'quantile', window=[1, 15])['answerability'] == 'unanswerable'
    assert summary.answer(result, 'count', window=result['sources'][0]['time_bounds'])['value'] == 2
    assert summary.answer(result, 'causality')['answerability'] == 'unanswerable'
    assert summary.answer(result, 'count', phase='absent')['answerability'] == 'unanswerable'
    other = copy.deepcopy(result)
    other['sources'][0]['identity']['node'] = 'another-node'
    merged = summary.merge([result, other])
    assert summary.answer(merged, 'count', window=[0, 100])['answerability'] == 'unanswerable'


def test_missing_boundaries_remain_unknown_and_raw_retention_explicit(tmp_path):
    with Recorder(tmp_path, background_flush=False) as recorder:
        recorder.emit('span_end', {'span_id': 'missing'})
        recorder.emit('span_start', {'span_id': 'unfinished', 'name': 'x'})
    result = summary.summarize(read_trace(recorder.path))
    assert result['sources'][0]['unmatched_events'] == 2
    assert summary.answer(result, 'count')['answerability'] == 'unanswerable'
    assert 'unchanged' in result['retention']['raw']


def test_many_adversarial_values_and_merge_order(tmp_path):
    rng = random.Random(914)
    values = [rng.randrange(1 << rng.randrange(1, 55)) for _ in range(1000)]
    _, snapshot = fixture(tmp_path, values)
    document = summary.summarize(snapshot)
    for q in [i / 100 for i in range(101)]:
        result = summary.answer(document, 'quantile', quantile=q)
        actual = sorted(values)[max(0, math.ceil(q * len(values)) - 1)]
        assert result['lower'] <= actual <= result['upper']


@pytest.mark.parametrize('mutation', [lambda d: d.update(parameters={}),
    lambda d: d['sources'][0].update(first_seq=True),
    lambda d: d['sources'][0]['groups'][0].update(count=999),
    lambda d: d['sources'][0]['groups'][0].update(bins=[[0, 100, 1]]),
    lambda d: d['sources'][0]['groups'][0].update(top=[False]),
    lambda d: d['sources'].append(None)])
def test_untrusted_summary_validation(tmp_path, mutation):
    _, snapshot = fixture(tmp_path, [100])
    result = summary.summarize(snapshot)
    mutation(result)
    with pytest.raises(ValueError):
        summary.validate(result)


def test_cli_complete_private_create_query_delete_and_no_clobber(tmp_path):
    path, _ = fixture(tmp_path, [1, 2])
    output = tmp_path / 'summary.json'
    command = [sys.executable, '-m', 'raven.trace_recorder', 'summary']
    run = subprocess.run(command + ['create', str(path), '--output', str(output)], text=True, capture_output=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    result = subprocess.run(command + ['query', str(output), 'count'], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout)['value'] == 2
    repeated = subprocess.run(command + ['create', str(path), '--output', str(output)], text=True, capture_output=True, timeout=10)
    assert repeated.returncode == 2 and output.read_bytes() == original
    digest = json.loads(original)['sources'][0]['digest']
    result = subprocess.run(command + ['delete', str(output), '--digest', digest, '--output', str(tmp_path / 'deleted.json')], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / 'deleted.json').read_text())['sources'] == []
    assert path.exists()


def test_query_scope_is_canonical_and_descriptive_extensions_are_ignored(tmp_path):
    _, snapshot = fixture(tmp_path, [100])
    result = summary.summarize(snapshot)
    result.pop('scope')
    result['extension'] = {'description': 'future descriptive metadata'}
    assert summary.answer(result, 'count')['scope'] == summary.SCOPE
    result['scope'] = 'fabricated whole-run completeness'
    assert summary.answer(result, 'count')['scope'] == summary.SCOPE


@pytest.mark.parametrize('field,value', [('coverage', {}), ('coverage', {'scope': 'x', 'complete': 'yes'}),
    ('source_status', 'INVALID'), ('source_status', []), ('warnings', [7]), ('evicted', True), ('unmatched_events', -1)])
def test_provenance_shape_is_checked_before_querying(tmp_path, field, value):
    _, snapshot = fixture(tmp_path, [100])
    result = summary.summarize(snapshot)
    result['sources'][0][field] = value
    with pytest.raises(ValueError):
        summary.validate(result)


def test_top_k_cannot_contradict_bin_frequency_or_exact_small_sample(tmp_path):
    _, snapshot = fixture(tmp_path / 'large', list(range(100, 140)))
    result = summary.summarize(snapshot)
    group = result['sources'][0]['groups'][0]
    group['top'][2] = 139
    group['top'].sort(reverse=True)
    with pytest.raises(ValueError, match='contradict'):
        summary.validate(result)
    _, snapshot = fixture(tmp_path / 'small', [128, 129])
    result = summary.summarize(snapshot)
    result['sources'][0]['groups'][0]['top'] = [129, 129]
    with pytest.raises(ValueError, match='contradict'):
        summary.validate(result)
