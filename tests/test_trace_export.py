import json
import os
import subprocess
import sys

import pytest

from raven import trace_export
from raven.trace_recorder import Recorder
from raven.trace_protocol import read_trace


def fixture(tmp_path):
    recorder = Recorder(tmp_path, job_id='5', attempt='attempt-one', background_flush=False,
                        coverage={'adapter': 'python-sys-monitoring', 'scope': 'explicit test frames'})
    for kind, name in [('call', 'outer'), ('call', 'inner'), ('yield', 'inner'), ('resume', 'inner'),
                       ('unwind', 'inner'), ('return', 'outer')]:
        recorder.emit(kind, {'frame': name, 'function': name, 'file': '/project/program.py', 'line': 3}, thread='one')
    with recorder.span('cooperative'):
        pass
    recorder.close()
    return recorder.path


def assert_balanced(document):
    assert all(profile['type'] == 'evented' for profile in document['profiles'])
    for profile in document['profiles']:
        stack, previous = [], -1
        for event in profile['events']:
            assert event['at'] >= previous
            previous = event['at']
            if event['type'] == 'O':
                stack.append(event['frame'])
            else:
                assert stack.pop() == event['frame']
        assert not stack


def test_perfetto_preserves_intervals_instrumented_identity_and_non_frame_evidence(tmp_path):
    path = fixture(tmp_path)
    document = trace_export.convert(read_trace(path))
    spans = [event for event in document['traceEvents'] if event['ph'] == 'X']
    assert [event['name'] for event in spans] == ['outer', 'inner', 'inner', 'cooperative']
    assert all(event['dur'] >= 0 and event['pid'] == os.getpid() for event in spans)
    assert document['raven']['identity']['attempt'] == 'attempt-one'
    assert document['raven']['source_status'] == 'COMPLETE'
    assert document['raven']['unpaired_frame_events'] == 0
    assert any(event['name'] == 'stream_end' for event in document['traceEvents'])


def test_speedscope_is_balanced_evented_wall_intervals_not_sampled_cpu(tmp_path):
    document = trace_export.convert(read_trace(fixture(tmp_path)), 'speedscope')
    assert_balanced(document)
    assert [frame['name'] for frame in document['shared']['frames']] == ['outer', 'inner']
    assert len(document['profiles'][0]['events']) == 6
    assert 'not sampled CPU' in ' '.join(document['raven']['limitations'])


def test_unknown_prefix_and_unclosed_tail_stay_unknown(tmp_path):
    with Recorder(tmp_path, background_flush=False,
                  coverage={'adapter': 'python-profile', 'scope': 'legacy test'}) as recorder:
        for kind, name in [('return', 'unknown'), ('call', 'open'), ('call', 'pair'), ('return', 'pair')]:
            recorder.emit(kind, {'frame': name, 'function': name}, thread='one')
    document = trace_export.convert(read_trace(recorder.path), 'speedscope')
    assert_balanced(document)
    assert [frame['name'] for frame in document['shared']['frames']] == ['pair']
    assert document['raven']['unpaired_frame_events'] == 2


def test_live_prefix_is_labelled_and_never_reads_uncommitted_tail(tmp_path):
    recorder = Recorder(tmp_path, background_flush=False, flush_events=65536, flush_interval=60)
    try:
        recorder.flush()
        recorder.emit('private-uncommitted-event')
        document = trace_export.convert(read_trace(recorder.path))
        assert document['raven']['source_status'] == 'LIVE'
        assert 'private-uncommitted-event' not in json.dumps(document)
    finally:
        recorder.close()


@pytest.mark.parametrize('format', ['perfetto', 'speedscope'])
def test_cli_writes_new_private_file_and_refuses_overwrite(tmp_path, format):
    source = fixture(tmp_path)
    output = tmp_path / (format + '.json')
    argv = [sys.executable, '-m', 'raven.trace_recorder', 'export', str(source), '--format', format, '--output', str(output)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    original = output.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(result.stdout)['read_seq'] == read_trace(source)['read_seq']
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert output.read_bytes() == original


def test_managed_export_is_read_only_admitted_and_has_inspect_budget(tmp_path, monkeypatch):
    from raven.raven_execution import cli_admission, ExecutionRouter
    source = fixture(tmp_path)
    captured = {}
    original = trace_export.protocol.read_trace
    def read(path, **kwargs):
        captured.update(kwargs)
        return original(path, **kwargs)
    monkeypatch.setattr(trace_export.protocol, 'read_trace', read)
    assert cli_admission(['trace', 'export', str(source), '--output', 'new.json'], ExecutionRouter({'managed': True})) is None
    trace_export.export(source, tmp_path / 'new.json', managed=True)
    assert captured == {'max_events': 512, 'max_bytes': 256 << 10}


def test_invalid_journal_and_unsupported_stack_semantics_refuse_export(tmp_path):
    with Recorder(tmp_path, background_flush=False) as recorder:
        recorder.emit('call', {'frame': 'f', 'function': 'f'})
        recorder.emit('return', {'frame': 'f'})
    with pytest.raises(ValueError, match='recorded Python'):
        trace_export.convert(read_trace(recorder.path), 'speedscope')
    journal = recorder.directory / 'events.jsonl'
    journal.write_bytes(journal.read_bytes().replace(b'"function":"f"', b'"function":"g"'))
    with pytest.raises(ValueError, match='invalid trace'):
        trace_export.export(recorder.path, tmp_path / 'bad.json')
    assert not (tmp_path / 'bad.json').exists()


def test_legal_descriptive_data_cannot_break_export_or_override_source_sequence(tmp_path):
    with Recorder(tmp_path, background_flush=False) as recorder:
        recorder.emit('note', {'span_id': {'unknown': True}, 'seq': 9999})
    result = trace_export.convert(read_trace(recorder.path))
    note = next(event for event in result['traceEvents'] if event['name'] == 'note')
    assert note['args']['seq'] == 2


def test_failed_export_does_not_publish_partial_data_or_replace_a_symlink(tmp_path, monkeypatch):
    source = fixture(tmp_path)
    target = tmp_path / 'keep.json'
    target.write_text('keep')
    link = tmp_path / 'link.json'
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        trace_export.export(source, link)
    assert target.read_text() == 'keep' and link.is_symlink()
    def failed(_):
        raise OSError('injected disk failure')
    monkeypatch.setattr(trace_export.os, 'fsync', failed)
    with pytest.raises(OSError, match='disk failure'):
        trace_export.export(source, tmp_path / 'partial.json')
    assert not (tmp_path / 'partial.json').exists()
    assert not list(tmp_path.glob('.raven-trace-*'))
