import base64
import json
import os
from pathlib import Path

import pytest

from raven.terminal_recording import Recorder, read_recording, replay, MAX_FILE, SCHEMA
from raven.terminal_fusion import recording_text


def private(path, data):
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def test_recording_round_trip_output_resize_and_inert_replay(tmp_path):
    path = tmp_path / 'recording.jsonl'
    clock = [100.0]
    recorder = Recorder(path, 'session', cols=80, rows=24, clock=lambda: clock[0])
    recorder.output(b'hello\r\n\x1b[31mred\x1b[0m')
    clock[0] += .5
    recorder.resize(120, 40)
    recorder.output('λ界'.encode())
    recorder.close()
    assert path.stat().st_mode & 0o777 == 0o600
    header, events = read_recording(path)
    assert header['input_recorded'] is False
    assert events[0]['data'] == b'hello\r\n\x1b[31mred\x1b[0m'
    assert events[1] == {'event': 'resize', 't': .5, 'cols': 120, 'rows': 40}
    class Renderer:
        def __init__(self): self.calls = []
        def resize(self, *args): self.calls.append(('resize', args))
        def feed(self, data): self.calls.append(('feed', data))
    renderer = Renderer()
    result = replay(path, renderer)
    assert result['execution'] == 'none'
    assert renderer.calls[0] == ('resize', (80, 24))
    assert renderer.calls[-1] == ('feed', 'λ界'.encode())
    text = recording_text(events)
    assert '\x1b' not in text and 'red' in text


@pytest.mark.parametrize('budget', ['bytes', 'events', 'time'])
def test_recorder_stops_visibly_at_budget(tmp_path, budget):
    clock = [1.0]
    kwargs = {'clock': lambda: clock[0]}
    if budget == 'bytes': kwargs['max_bytes'] = 1024
    if budget == 'events': kwargs['max_events'] = 1
    if budget == 'time': kwargs['max_seconds'] = 1
    record = Recorder(tmp_path / 'record', 'a', **kwargs)
    if budget == 'bytes': assert not record.output(b'x' * 1024)
    elif budget == 'events':
        assert record.output(b'x')
        assert not record.output(b'y')
    else:
        clock[0] = 3
        assert not record.output(b'x')
    assert record.closed and budget[:-1] in record.reason
    assert not record.output(b'later')
    read_recording(record.path)


def test_recorder_does_not_overwrite_or_follow_symlink(tmp_path):
    path = private(tmp_path / 'owned', b'existing')
    with pytest.raises(FileExistsError): Recorder(path, 'x')
    linked = tmp_path / 'link'
    linked.symlink_to(path)
    with pytest.raises(OSError): Recorder(linked, 'x')
    with pytest.raises(OSError): read_recording(linked)
    assert path.read_bytes() == b'existing'


def test_shared_recording_permissions_rejected(tmp_path):
    path = private(tmp_path / 'record', b'{}\n')
    path.chmod(0o644)
    with pytest.raises(ValueError, match='private'): read_recording(path)


@pytest.mark.parametrize('event', [
    {'event':'input', 't':0, 'data':'cm0gLXJmIC8='},
    {'event':'execute', 't':0, 'command':'touch /tmp/must-not-exist'},
    {'event':'output', 't':float('nan'), 'data':''},
    {'event':'output', 't':-1, 'data':''},
    {'event':'output', 't':True, 'data':''},
    {'event':'output', 't':999999, 'data':''},
    {'event':'output', 't':0, 'data':'?'},
    {'event':'resize', 't':0, 'cols':0, 'rows':80},
    {'event':'resize', 't':0, 'cols':True, 'rows':80},
    {'event':'resize', 't':0, 'cols':5000, 'rows':80},
    [], 'not an event',
])
def test_malformed_recording_cannot_replay(tmp_path, event):
    header = {'schema':SCHEMA, 'cols':80, 'rows':24, 'input_recorded':False}
    path = private(tmp_path / 'record', (json.dumps(header)+'\n'+json.dumps(event)+'\n').encode())
    with pytest.raises(ValueError): read_recording(path)


def test_replay_validates_all_events_before_paint(tmp_path):
    path = tmp_path / 'record'
    record = Recorder(path, 'x')
    record.output(b'echo harmless')
    record.close()
    with path.open('ab') as stream:
        stream.write(b'{"event":"execute","t":100}\n')
    class Renderer:
        def resize(self, *_): pytest.fail('must validate all data before replay')
        def feed(self, *_): pytest.fail('must validate all data before replay')
    with pytest.raises(ValueError): replay(path, Renderer())


def test_short_writes_are_completed(tmp_path, monkeypatch):
    original = os.write
    monkeypatch.setattr(os, 'write', lambda fd, data: original(fd, data[:7]))
    record = Recorder(tmp_path / 'record', 'a')
    record.output(b'x' * 100)
    record.close()
    _, events = read_recording(record.path)
    assert events[0]['data'] == b'x' * 100


def test_write_failure_closes_descriptor(tmp_path, monkeypatch):
    record = Recorder(tmp_path / 'record', 'a')
    def fail(*_): raise OSError('disk full')
    monkeypatch.setattr(os, 'write', fail)
    with pytest.raises(OSError): record.output(b'foo')
    assert record.closed and record.fd == -1 and record.reason == 'recording write failed'


def test_oversize_file_rejected_before_read(tmp_path):
    path = private(tmp_path / 'record', b'')
    with path.open('wb') as stream: stream.truncate(MAX_FILE+1)
    with pytest.raises(ValueError, match='byte budget'): read_recording(path)


def test_event_times_must_not_move_backwards(tmp_path):
    header = {'schema':SCHEMA, 'cols':80, 'rows':24, 'input_recorded':False}
    events = [{'event':'output','t':t,'data':base64.b64encode(b'hi').decode()} for t in (2,1)]
    path = private(tmp_path / 'record', '\n'.join(json.dumps(x) for x in [header]+events).encode())
    with pytest.raises(ValueError, match='ordered'): read_recording(path)


def test_large_output_is_chunked_without_losing_bytes(tmp_path):
    record = Recorder(tmp_path / 'record', 'x')
    data = bytes(range(256))*600
    record.output(data)
    record.close()
    _, events = read_recording(record.path)
    assert len(events) == 3
    assert b''.join(event['data'] for event in events) == data


@pytest.mark.parametrize('payload', [b'\x1b]52;c;YWJj\x07tail', b'\x1b[?1003hfoo', b'\x1bPmalicious\x1b\\end', b'\x00\x1b[2Jx\x08'])
def test_plain_replay_cannot_change_outer_terminal_modes(payload):
    text = recording_text([{'event':'output','data':payload}])
    assert all(char.isprintable() or char in '\n\t' for char in text)


def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    path=tmp_path/'fifo'
    os.mkfifo(path,0o600)
    with pytest.raises(ValueError,match='regular file'):
        read_recording(path)


def test_huge_integer_timestamp_is_rejected_without_overflow(tmp_path):
    header={'schema':SCHEMA,'cols':80,'rows':24,'input_recorded':False}
    event={'event':'output','t':10**500,'data':''}
    path=private(tmp_path/'record',(json.dumps(header)+'\n'+json.dumps(event)+'\n').encode())
    with pytest.raises(ValueError,match='ordered'):read_recording(path)
