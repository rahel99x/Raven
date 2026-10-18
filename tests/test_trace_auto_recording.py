"""Block integrity, bounded streaming and source filtering for automatic capture."""
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tracemalloc
import weakref

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from raven import trace_protocol as P
from raven.trace_recorder import PythonCapture, Recorder


def recorder(tmp_path, **kwargs):
    return Recorder(tmp_path, journal_format='blocks', background_flush=False,
                    flush_interval=60, **kwargs)


def test_blocks_hash_once_per_flush_and_preserve_small_pages(tmp_path, monkeypatch):
    calls = []
    original = P.hashlib.sha256
    def digest(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(P.hashlib, 'sha256', digest)
    writes = []
    original_writev = os.writev
    def writev(fd, vectors):
        writes.append(len(vectors))
        return original_writev(fd, vectors)
    monkeypatch.setattr(os, 'writev', writev)
    with recorder(tmp_path, flush_events=64) as stream:
        for n in range(140):
            assert stream.emit('sample', {'n': n})
    assert len(calls) == 3 and writes == [65, 65, 15]
    reader = P.TraceReader(stream.path, retain=7)
    seen = []
    previous = 0
    for page in reader.iter_pages(max_events=7, max_bytes=1024):
        seen.extend(e['seq'] for e in page['events'] if e['seq'] > previous)
        previous = page['read_seq']
        assert len(page['events']) <= 7
    assert page['complete'] and seen == list(range(1, 143))
    assert page['window_start'] == 136 and page['window_end'] == 142


def test_process_metadata_round_trip_and_typed_identity(tmp_path):
    fields = dict(parent_pid=os.getppid(), role='rank', launcher='torchrun', rank=0,
                  local_rank=0, world_size=2, interpreter='CPython 3.12')
    with recorder(tmp_path, identity_extra=fields) as stream:
        stream.emit('sample')
    value = P.read_trace(stream.path)
    assert value['complete']
    assert value['manifest']['process'] == value['process'] == dict(fields, pid=os.getpid())
    assert all(value['identity'][key] == data for key, data in fields.items())
    for key, data in {'parent_pid': True, 'rank': -1, 'world_size': 0, 'launcher': '\x1b[0m'}.items():
        with pytest.raises(ValueError):
            P.validate_identity(dict(value['identity'], **{key: data}))


@pytest.mark.parametrize('damage', ['payload', 'header', 'truncation', 'sequence', 'format'])
def test_corrupt_blocks_do_not_certify_or_publish_the_damaged_block(tmp_path, damage):
    with recorder(tmp_path, flush_events=4) as stream:
        for n in range(12):
            stream.emit('sample', {'n': n})
    path = stream.directory / 'events.jsonl'
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    if damage == 'payload':
        record = P.decode_json(lines[7]); record['event']['data']['n'] = 9
        lines[7] = P.encoded(record) + b'\n'
    elif damage == 'header':
        header = P.decode_json(lines[5]); header['prev'] = '0' * 64
        lines[5] = P.encoded(header) + b'\n'
    elif damage == 'truncation':
        lines[-1] = lines[-1][:-10]
    elif damage == 'sequence':
        header = P.decode_json(lines[5])
        record = P.decode_json(lines[7]); record['seq'] = 999
        lines[7] = P.encoded(record) + b'\n'
        lines[5], _ = P.encode_block(stream.stream, header['first_seq'], header['prev'], lines[6:10])
    else:
        manifest = P.read_json(stream.path); manifest['journal_format'] = 'future/unknown'
        stream.path.write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match='format'):
            P.read_trace(stream.path)
        return
    path.write_bytes(b''.join(lines))
    value = P.read_trace(stream.path)
    assert value['status'] == 'INVALID' and not value['complete']
    assert value['read_seq'] < 14
    if damage in {'payload', 'header'}:
        assert value['read_seq'] == 4


def test_torn_tail_is_uncommitted_even_if_manifest_was_closed(tmp_path):
    with recorder(tmp_path) as stream:
        stream.emit('sample')
    with (stream.directory / 'events.jsonl').open('ab') as target:
        target.write(b'{"schema":"raven.trace-block/v1"')
    value = P.read_trace(stream.path)
    assert value['read_seq'] == 3 and not value['complete']
    assert any('uncommitted tail' in warning for warning in value['warnings'])


def test_partial_write_never_advances_durable_checkpoint(tmp_path, monkeypatch):
    stream = recorder(tmp_path)
    stream.flush()
    before = P.read_json(stream.path)
    stream.emit('sample', {'n': 1})
    writev = os.writev
    calls = []
    def broken(fd, vectors):
        if not calls:
            calls.append(1)
            return writev(fd, [vectors[0][:10]])
        raise OSError('simulated full filesystem')
    monkeypatch.setattr(os, 'writev', broken)
    stream._fail('ERROR', 'simulated failed flush')
    stream.close()
    after = P.read_json(stream.path)
    assert after['committed_seq'] == before['committed_seq']
    assert after['committed_bytes'] == before['committed_bytes']
    value = P.read_trace(stream.path)
    assert value['status'] == 'PARTIAL' and value['read_seq'] == 1
    assert any('uncommitted tail' in item for item in value['warnings'])


def test_blocks_budget_and_background_deadline(tmp_path):
    with Recorder(tmp_path, journal_format='blocks', max_bytes=4096,
                  flush_events=64, flush_interval=.01) as stream:
        accepted = [stream.emit('sample', {'text': 'x' * 800}) for _ in range(10)]
    value = P.read_trace(stream.path)
    assert any(accepted) and not all(accepted)
    assert not value['complete'] and value['manifest']['state'] == 'LIMIT'
    assert (stream.directory / 'events.jsonl').stat().st_size <= 4096
    with Recorder(tmp_path, journal_format='blocks', flush_interval=.01) as live:
        live.emit('call', {'function': 'sleeping'})
        for _ in range(100):
            snapshot = P.read_trace(live.path)
            if snapshot['read_seq'] >= 2:
                break
            threading.Event().wait(.01)
        assert snapshot['read_seq'] == 2 and snapshot['status'] == 'LIVE'


def test_scope_cache_is_bounded_weak_and_keeps_project_first_party(tmp_path):
    with recorder(tmp_path) as stream:
        capture = PythonCapture(stream, scope='project', roots=[tmp_path])
        project = compile('pass', str(tmp_path / 'src' / 'train.py'), 'exec')
        library = compile('pass', str(tmp_path / '.venv' / 'site-packages' / 'torch.py'), 'exec')
        stdlib = compile('pass', '/usr/lib/python3.12/threading.py', 'exec')
        assert capture._included(project)
        assert not capture._included(library) and not capture._included(stdlib)
        assert capture._included(compile('pass', '<string>', 'exec'))
        reference = weakref.ref(project)
        del project
        gc.collect()
        assert reference() is None
        codes = [compile('pass', str(tmp_path / f'{i}.py'), 'exec') for i in range(1100)]
        for code in codes:
            assert capture._included(code)
        assert len(capture._scope_cache) == 1024


@pytest.mark.parametrize('legacy', [True, False])
def test_source_filter_and_all_scope_in_real_interpreter(tmp_path, legacy):
    if not legacy and not hasattr(sys, 'monitoring'):
        pytest.skip('CPython 3.12 monitoring')
    project = tmp_path / 'project'; project.mkdir()
    external = tmp_path / 'external.py'
    external.write_text('def excluded(): return 9\n')
    source = '''
import importlib.util, sys
from raven.trace_recorder import Recorder, PythonCapture
from raven.trace_protocol import read_trace
spec = importlib.util.spec_from_file_location('external', sys.argv[2])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def included(): return module.excluded()
for scope in ('project', 'all'):
    with Recorder(sys.argv[1], journal_format='blocks', background_flush=False) as recorder:
        capture = PythonCapture(recorder, legacy=LEGACY, scope=scope, roots=[sys.argv[1]]).start()
        included()
        capture.stop()
    events = read_trace(recorder.path)['events']
    functions = {e['data'].get('function') for e in events}
    assert 'included' in functions, functions
    assert ('excluded' in functions) == (scope == 'all'), functions
'''.replace('LEGACY', repr(legacy))
    app = project / 'app.py'; app.write_text(source)
    result = subprocess.run([sys.executable, str(app), str(project), str(external)],
                            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not hasattr(sys, 'monitoring'), reason='CPython 3.12 monitoring')
def test_disable_only_applies_to_permanently_excluded_code(tmp_path):
    with recorder(tmp_path) as stream:
        capture = PythonCapture(stream, scope='project', roots=[tmp_path]).start()
        try:
            outside = compile('pass', '/external/not_project.py', 'exec')
            inside = compile('pass', str(tmp_path / 'program.py'), 'exec')
            callback = capture.callbacks[sys.monitoring.events.PY_START]
            assert callback(outside, 0) is sys.monitoring.DISABLE
            capture.local.busy = True
            assert callback(inside, 0) is not sys.monitoring.DISABLE
            capture.local.busy = False
        finally:
            capture.stop()


@given(st.lists(st.sampled_from(['emit', 'flush', 'fork', 'spawn', 'exit']), min_size=1, max_size=35))
@settings(max_examples=16, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_arbitrary_process_stream_lifecycles_remain_independent(tmp_path, actions):
    """Model interleaved owned streams; actual OS fork/spawn is tested by launch."""
    streams = [recorder(tmp_path, flush_events=5)]
    active = streams[0]
    for action in actions:
        if action in {'fork', 'spawn'}:
            active = recorder(tmp_path, flush_events=5, identity_extra={'parent_pid': os.getpid(), 'role': 'helper'})
            streams.append(active)
        elif action == 'exit':
            active.close()
            active = streams[0]
        elif action == 'flush':
            active.flush()
        else:
            active.emit('sample', {'action': action})
    for stream in streams:
        stream.close()
        value = P.read_trace(stream.path)
        assert value['complete'], value['warnings']
        assert [event['seq'] for event in value['events']] == list(range(1, value['read_seq'] + 1))


def test_aggregate_reservation_is_per_block_and_refusal_keeps_checkpoint(tmp_path):
    calls = []
    def reserve(size):
        calls.append(size)
        return len(calls) == 1
    with recorder(tmp_path, flush_events=4, reserve_bytes=reserve) as stream:
        assert stream.emit('sample')
        assert stream.emit('sample')
        assert stream.emit('sample')
        before = P.read_json(stream.path)
        for _ in range(4):
            accepted = stream.emit('sample')
        assert accepted is False
    value = P.read_trace(stream.path)
    assert len(calls) == 2
    assert value['manifest']['state'] == 'LIMIT' and value['read_seq'] == 4
    assert value['manifest']['committed_bytes'] == before['committed_bytes'] == calls[0]
    assert value['manifest']['dropped'] == 4
    assert any('Shared run storage budget' in warning for warning in value['warnings'])


def test_aggregate_reservation_error_is_capture_failure_not_application_failure(tmp_path):
    def broken(size):
        raise ValueError('unavailable budget')
    with recorder(tmp_path, reserve_bytes=broken) as stream:
        stream.emit('sample')
    result = P.read_trace(stream.path)
    assert result['manifest']['state'] == 'ERROR'
    assert result['read_seq'] == 0 and not result['complete']
    assert any('reservation failed: ValueError' in warning for warning in result['warnings'])


def test_reader_streams_200_mb_with_bounded_python_memory(tmp_path):
    """Real 200 MB disk fixture, bounded allocations; no whole-file read."""
    with recorder(tmp_path) as seed:
        pass
    manifest = P.read_json(seed.path)
    path = seed.directory / 'events.jsonl'
    sequence = 0
    previous = '0' * 64
    total = 0
    with path.open('wb') as output:
        while total < 200_000_000:
            lines = []
            first = sequence + 1
            for _ in range(32):
                sequence += 1
                event = dict(kind='sample', mono_ns=sequence, thread='main',
                             data={'a': 'x' * 8192, 'b': 'y' * 8192})
                lines.append(P.encoded(dict(schema=P.EVENT_FORMAT, stream=seed.stream,
                                            seq=sequence, event=event)) + b'\n')
            header, previous = P.encode_block(seed.stream, first, previous, lines)
            output.write(header)
            output.writelines(lines)
            total += len(header) + sum(map(len, lines))
        sequence += 1
        line = P.encoded(dict(schema=P.EVENT_FORMAT, stream=seed.stream, seq=sequence,
                              event=dict(kind='stream_end', mono_ns=sequence, thread='main', data={}))) + b'\n'
        header, previous = P.encode_block(seed.stream, sequence, previous, [line])
        output.write(header); output.write(line)
        total += len(header) + len(line)
    manifest.update(committed_bytes=total, committed_seq=sequence, state='CLOSED')
    seed.path.write_text(json.dumps(manifest))
    assert path.stat().st_size >= 200_000_000
    gc.collect()
    tracemalloc.start()
    try:
        reader = P.TraceReader(seed.path, retain=32)
        observed = 0
        for page in reader.iter_pages(max_events=32, max_bytes=1 << 20):
            fresh = [e for e in page['events'] if e['seq'] > observed]
            assert [e['seq'] for e in fresh] == list(range(observed + 1, page['read_seq'] + 1))
            observed = page['read_seq']
            assert len(page['events']) <= 32
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page['complete'] and observed == sequence
    assert peak < 12 << 20, f'Python allocations peaked at {peak} bytes for {total} journal bytes'


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='POSIX fork')
@given(st.lists(st.sampled_from(['emit', 'flush', 'fork', 'spawn']), min_size=1, max_size=7))
@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_random_real_fork_spawn_exit_flush_preserves_sequence(tmp_path, actions):
    source = r'''
import json, os, subprocess, sys
from raven.trace_recorder import Recorder
from raven.trace_protocol import discover_trace, read_trace
root, actions = sys.argv[1], json.loads(sys.argv[2])
with Recorder(root, journal_format='blocks', background_flush=False, flush_events=3, attempt='property') as parent:
    for action in actions:
        if action == 'fork':
            pid = os.fork()
            if pid == 0:
                assert parent.emit('unsafe-child') is False
                with Recorder(root, journal_format='blocks', background_flush=False, attempt='property') as child:
                    child.emit('child'); child.flush(); child.emit('child')
                os._exit(0)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        elif action == 'spawn':
            command = 'from raven.trace_recorder import Recorder; import sys; r=Recorder(sys.argv[1],journal_format="blocks",background_flush=False,attempt="property"); r.emit("child"); r.flush(); r.close()'
            subprocess.run([sys.executable, '-c', command, root], check=True, timeout=10)
        elif action == 'flush':
            parent.flush()
        else:
            parent.emit('parent')
for path in discover_trace(root, 'local', 'property'):
    value = read_trace(path)
    assert value['complete'], value['warnings']
    assert [e['seq'] for e in value['events']] == list(range(1, value['read_seq'] + 1))
    assert all(e['kind'] != 'unsafe-child' for e in value['events'])
'''
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path), json.dumps(actions)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_retained_window_has_byte_and_event_bounds_without_skipping_page_events(tmp_path):
    with recorder(tmp_path, flush_events=8) as stream:
        for index in range(120):
            stream.emit('sample', {'blob': 'x' * 8192, 'index': index})
    reader = P.TraceReader(stream.path, retain=1000, retain_bytes=128 << 10)
    previous = 0
    for page in reader.iter_pages(max_events=1000, max_bytes=8 << 20):
        fresh = [e['seq'] for e in page['events'] if e['seq'] > previous]
        assert fresh == list(range(previous + 1, page['read_seq'] + 1))
        assert page['retained_bytes'] <= 128 << 10
        previous = page['read_seq']
    assert page['complete'] and previous == 122
    assert len(page['events']) < 20 and page['evicted'] > 100


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='POSIX fork')
def test_fork_releases_inherited_capture_cache_locks_before_child_attachment(tmp_path):
    source = r'''
import os, sys, threading, warnings
warnings.simplefilter("ignore", DeprecationWarning)
from raven.trace_recorder import Recorder, PythonCapture
from raven.trace_protocol import read_trace
legacy = not hasattr(sys, 'monitoring')
parent = Recorder(sys.argv[1], journal_format='blocks', background_flush=False)
capture = PythonCapture(parent, legacy=legacy, scope='all').start()
ready_read, ready_write = os.pipe()
release_read, release_write = os.pipe()
begin_read, begin_write = os.pipe()
def hold():
    os.read(begin_read, 1)
    with capture._metadata_lock:
        os.write(ready_write, b"1")
        os.read(release_read, 1)
thread = threading.Thread(target=hold); thread.start()
os.write(begin_write, b"1")
assert os.read(ready_read, 1) == b"1"
assert not capture._metadata_lock.acquire(blocking=False), "cache lock must be held at fork"
pid = os.fork()
if pid == 0:
    capture.stop()
    child = Recorder(sys.argv[1], journal_format='blocks', background_flush=False)
    fresh = PythonCapture(child, legacy=legacy, scope='project', roots=[sys.argv[1]]).start()
    exec(compile('def child_work(): return 7\nchild_work()\n', '<string>', 'exec'))
    fresh.stop(); child.close()
    value = read_trace(child.path)
    assert value['complete'], value['warnings']
    assert any(e['data'].get('function') == 'child_work' for e in value['events'])
    os._exit(0)
os.write(release_write, b"1"); thread.join(5)
capture.stop(); parent.close()
_, status = os.waitpid(pid, 0)
assert os.waitstatus_to_exitcode(status) == 0
'''
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('legacy', [True, False])
def test_client_watch_cooperates_with_real_trace_capture(tmp_path, legacy):
    if not legacy and not hasattr(sys, 'monitoring'):
        pytest.skip('CPython 3.12 monitoring')
    source = r'''
import json, pathlib, sys
from raven import client
from raven.trace_recorder import Recorder, PythonCapture
from raven.trace_protocol import read_trace
root = pathlib.Path(sys.argv[1])
client.configure(path=root / 'metrics.jsonl')
def scientific_step():
    loss = 3.0
    return loss
recorder = Recorder(root, journal_format='blocks', background_flush=False)
capture = PythonCapture(recorder, legacy=LEGACY, scope='project', roots=[root]).start()
with client.watch(scientific_step, 'loss', kind='objective'):
    assert scientific_step() == 3.0
capture.stop(); recorder.close()
assert client._get_client().flush()
client._shutdown()
records = [json.loads(line) for line in (root / 'metrics.jsonl').read_text().splitlines()]
assert any(record['metrics'].get('loss') == 3.0 for record in records)
snapshot = read_trace(recorder.path)
assert snapshot['complete'], snapshot['warnings']
assert any(event['kind'] == 'call' and event['data'].get('function') == 'scientific_step' for event in snapshot['events'])
assert any(event['kind'] == 'return' and event['data'].get('function') == 'scientific_step' for event in snapshot['events'])
assert sys.getprofile() is None
'''.replace('LEGACY', repr(legacy))
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@given(st.text(max_size=200))
@settings(max_examples=100, deadline=None)
def test_fast_control_validation_matches_exact_original_semantics(value):
    forbidden = any(ord(character) < 32 or ord(character) == 127 for character in value)
    if forbidden:
        with pytest.raises(ValueError, match='bounded printable'):
            P._bounded_data(value)
    else:
        P._bounded_data(value)


def test_reader_refuses_regression_even_inside_an_unconsumed_block(tmp_path):
    with recorder(tmp_path, flush_events=64) as stream:
        for _ in range(12):
            stream.emit('sample')
    reader = P.TraceReader(stream.path, retain=2)
    assert reader.refresh(max_events=1)['read_seq'] == 1
    manifest = P.read_json(stream.path)
    manifest['committed_seq'] -= 1
    stream.path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='checkpoint regressed'):
        reader.refresh(max_events=1)


@pytest.mark.parametrize('options', [{'flush_events': True}, {'flush_events': 64.0},
                                     {'max_bytes': 4096.0}, {'flush_interval': True}])
def test_recorder_rejects_budget_types_that_readers_cannot_validate(tmp_path, options):
    with pytest.raises(ValueError, match='budget'):
        Recorder(tmp_path, journal_format='blocks', **options)


def test_legacy_reservation_failure_does_not_escape_instrumentation(tmp_path):
    def broken(size):
        raise RuntimeError('budget unavailable')
    with Recorder(tmp_path, reserve_bytes=broken, background_flush=False) as stream:
        assert not stream.emit('sample')
    snapshot = P.read_trace(stream.path)
    assert snapshot['manifest']['state'] == 'ERROR' and snapshot['read_seq'] == 0
    assert any('reservation failed: RuntimeError' in warning for warning in snapshot['warnings'])


def test_terminal_capture_state_does_no_scope_or_metadata_work(tmp_path, monkeypatch):
    with recorder(tmp_path) as stream:
        capture = PythonCapture(stream, legacy=True, scope='project', roots=[tmp_path])
        stream._fail('LIMIT', 'test budget exhaustion')
        def unexpected(code):
            pytest.fail('terminal capture attempted metadata work')
        monkeypatch.setattr(capture, '_included', unexpected)
        monkeypatch.setattr(capture, '_code_metadata', unexpected)
        capture._event('call', unexpected.__code__)


@pytest.mark.skipif(not hasattr(sys, 'monitoring'), reason='CPython 3.12 monitoring')
def test_terminal_monitoring_disables_locations_without_scope_lookup(tmp_path, monkeypatch):
    with recorder(tmp_path) as stream:
        capture = PythonCapture(stream, scope='project', roots=[tmp_path]).start()
        try:
            stream._fail('LIMIT', 'test budget exhaustion')
            def unexpected(code):
                pytest.fail('terminal capture attempted a scope lookup')
            monkeypatch.setattr(capture, '_included', unexpected)
            callback = capture.callbacks[sys.monitoring.events.PY_START]
            assert callback(unexpected.__code__, 0) is sys.monitoring.DISABLE
            unwind = capture.callbacks[sys.monitoring.events.PY_UNWIND]
            assert unwind(unexpected.__code__, 0) is None
        finally:
            capture.stop()
