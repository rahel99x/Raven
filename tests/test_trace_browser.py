"""Offline trace views preserve verified identity, bounds and explicit actions."""
import hashlib
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from raven import trace_browser as browser, trace_export
from raven.trace_protocol import read_trace
from raven.trace_recorder import Recorder


def snapshot(tmp_path, *, hostile=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    with Recorder(tmp_path, job_id='42', attempt='exact-attempt', background_flush=False,
                  coverage={'adapter': 'python-profile', 'scope': 'explicit test frames'}) as recorder:
        for kind, frame in [('call', 'outer'), ('call', 'inner'), ('return', 'inner'), ('return', 'outer')]:
            name = '</script><img src=x onerror=alert(1)>\u2028&' if hostile and frame == 'inner' else frame
            recorder.emit(kind, {'frame': frame, 'function': name, 'file': '/project/test.py', 'line': 4}, thread='one')
        recorder.emit('note', {'name': '<svg onload=alert(2)>'}, thread='two')
    return read_trace(recorder.path), recorder.path


def document(raw):
    return json.loads(re.search(rb'<script id="trace-data" type="application/json">(.*?)</script>', raw, re.S)[1])


def test_html_is_offline_csp_protected_and_hostile_labels_remain_inert(tmp_path):
    value, _ = snapshot(tmp_path, hostile=True)
    raw = browser.render(value)
    model = document(raw)
    assert model['identity']['attempt'] == 'exact-attempt'
    assert model['status'] == 'COMPLETE'
    assert model['read_seq'] == model['committed_seq'] == value['read_seq']
    assert '<img src=x' in model['intervals'][1]['name']
    assert b'<img src=x' not in raw and b'<svg onload' not in raw
    assert b'\\u003c/script\\u003e' in raw and b'\\u2028' in raw
    digest = base64.b64encode(hashlib.sha256(browser._SCRIPT.encode()).digest())
    assert b"script-src 'sha256-" + digest + b"'" in raw
    assert b"connect-src 'none'" in raw and b"default-src 'none'" in raw
    assert b'<script src=' not in raw and b'innerHTML' not in raw and b'eval(' not in raw
    assert b'fixed snapshot' in raw
    assert b'id="marker-body"' in raw and b'id="result-body"' in raw


def test_nanosecond_provenance_stays_exact_above_javascript_integer_precision(tmp_path):
    value, _ = snapshot(tmp_path)
    base = (1 << 62) + 17
    for index, event in enumerate(value['events']):
        event['mono_ns'] = base + index * 7
    result = document(browser.render(value))
    assert result['origin_ns'] == str(base)
    assert result['intervals'][0]['duration_ns'] == '21'
    assert result['intervals'][0]['start_ns'] == '7'
    assert result['intervals'][0]['plot_start_ms'] == 7 / 1e6


def test_private_atomic_publication_never_overwrites_and_cleans_failed_temporary(tmp_path, monkeypatch):
    value, _ = snapshot(tmp_path / 'trace')
    output = tmp_path / 'trace.html'
    result = browser.export(value, output)
    assert result['path'] == str(output) and result['format'] == 'html'
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        browser.export(value, output)
    assert output.read_bytes() == original
    linked = tmp_path / 'link.html'
    linked.symlink_to(output)
    with pytest.raises(FileExistsError):
        browser.export(value, linked)
    assert linked.is_symlink() and output.read_bytes() == original
    def fail(_):
        raise OSError('disk failure')
    monkeypatch.setattr(browser.os, 'fsync', fail)
    with pytest.raises(OSError, match='disk failure'):
        browser.export(value, tmp_path / 'failed.html')
    assert not (tmp_path / 'failed.html').exists()
    assert not list(tmp_path.glob('.raven-trace-*'))


def test_cancelled_invalid_empty_and_oversized_views_are_not_published(tmp_path):
    value, _ = snapshot(tmp_path / 'trace')
    event = threading.Event()
    event.set()
    with pytest.raises(InterruptedError):
        browser.export(value, tmp_path / 'cancel.html', cancel=event)
    with pytest.raises(ValueError, match='byte budget'):
        browser.export(value, tmp_path / 'huge.html', max_bytes=100)
    with pytest.raises(ValueError, match='invalid trace'):
        browser.export(dict(value, status='INVALID'), tmp_path / 'bad.html')
    with pytest.raises(ValueError, match='No verified'):
        browser.export(dict(value, events=[]), tmp_path / 'empty.html')
    assert not list(tmp_path.glob('*.html'))


def test_cli_html_export_keeps_existing_managed_read_budgets(tmp_path, monkeypatch):
    value, source = snapshot(tmp_path / 'trace')
    output = tmp_path / 'trace.html'
    captured = {}
    original = trace_export.protocol.TraceReader.iter_pages
    def read(reader, **kwargs):
        captured.update(kwargs)
        return original(reader, **kwargs)
    monkeypatch.setattr(trace_export.protocol.TraceReader, 'iter_pages', read)
    result = trace_export.export(source, output, format='html', managed=True)
    assert 1 <= captured['max_events'] <= 512
    assert captured['max_bytes'] == 256 << 10
    assert result['source_status'] == 'COMPLETE'
    command = [sys.executable, '-m', 'raven.trace_recorder', 'export', str(source), '--format', 'html', '--output', str(tmp_path / 'cli.html')]
    run = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)['format'] == 'html'
    assert document((tmp_path / 'cli.html').read_bytes())['identity'] == value['identity']


@pytest.mark.parametrize('boundary', ['gap', 'lost', 'drop', 'loss', 'dropped', 'stack_checkpoint'])
def test_loss_and_stack_checkpoints_never_invent_cross_boundary_frame_or_span_durations(tmp_path, boundary):
    with Recorder(tmp_path, background_flush=False, coverage={'adapter': 'python-profile', 'scope': 'test'}) as recorder:
        for kind, data in [('call', {'frame': 'safe', 'function': 'safe'}), ('return', {'frame': 'safe'}),
                           ('call', {'frame': 'unknown', 'function': 'unknown'}),
                           ('span_start', {'span_id': 's', 'name': 'unknown-span'}),
                           (boundary, {'frames': []}), ('return', {'frame': 'unknown'}),
                           ('span_end', {'span_id': 's'})]:
            recorder.emit(kind, data, thread='one')
    value = read_trace(recorder.path)
    perfetto = trace_export.convert(value)
    assert [event['name'] for event in perfetto['traceEvents'] if event['ph'] == 'X'] == (['safe', 'unknown-span'] if boundary == 'stack_checkpoint' else ['safe'])
    assert perfetto['raven']['unpaired_frame_events'] == 2
    speedscope = trace_export.convert(value, 'speedscope')
    assert [frame['name'] for frame in speedscope['shared']['frames']] == ['safe']
    model = document(browser.render(value))
    assert [item['name'] for item in model['intervals']] == (['safe', 'unknown-span'] if boundary == 'stack_checkpoint' else ['safe'])
    assert any(item['kind'] == boundary for item in model['markers'])


def test_browser_open_is_explicit_graphical_request_with_truthful_fallback(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(browser.shutil, 'which', lambda name: '/usr/bin/xdg-open')
    monkeypatch.setattr(browser.subprocess, 'Popen', lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(poll=lambda: None))
    path = tmp_path / 'trace #1.html'
    assert 'SSH host' in browser.open_browser(path, environ={'SSH_CONNECTION': 'yes', 'DISPLAY': ':0'})
    assert 'No graphical' in browser.open_browser(path, environ={})
    assert not calls
    assert 'requested' in browser.open_browser(path, environ={'DISPLAY': ':0'})
    args, options = calls.pop()
    assert args[0] == ['/usr/bin/xdg-open', path.as_uri()]
    assert options['start_new_session'] and options['stdin'] == subprocess.DEVNULL
    monkeypatch.setattr(browser.shutil, 'which', lambda _: None)
    assert 'No desktop browser opener' in browser.open_browser(path, environ={'DISPLAY': ':0'})


class Hub:
    def start_task(self, work, done):
        self.work, self.done = work, done
        return True


def test_ui_action_exports_off_paint_and_only_opens_after_exact_context_completion(tmp_path, monkeypatch):
    from raven import job_panels, trace_ui
    value, _ = snapshot(tmp_path / 'trace')
    hub = Hub()
    context = {'key': ('job', 'attempt'), 'job_id': '42', 'light': False}
    app = SimpleNamespace(state_dir=str(tmp_path / 'state'), say=lambda text: messages.append(text), fail=lambda text: messages.append(text))
    messages, opened = [], []
    monkeypatch.setattr(job_panels, '_worker', lambda *args: hub)
    monkeypatch.setattr(trace_ui, 'cancel', browser.cancel)
    monkeypatch.setattr(trace_ui, '_context', lambda _: context)
    monkeypatch.setattr(browser, 'open_browser', lambda path: opened.append(path) or 'Browser open requested')
    assert browser.request(app, value, context)
    assert not opened and not list(tmp_path.glob('state/exports/*.html'))
    result = hub.work()
    assert not opened and Path(result['path']).exists()
    hub.done(result)
    assert opened == [result['path']]
    assert app.trace_browser_state['path'] == result['path']
    assert any('Trace HTML:' in message for message in messages)


@pytest.mark.parametrize('state_enabled', [False, True])
@pytest.mark.parametrize('configured', [False, True])
def test_browser_exports_use_durable_root_independent_of_state_and_workdir(tmp_path, monkeypatch, state_enabled, configured):
    from raven import job_panels, trace_ui
    value, _ = snapshot(tmp_path / 'trace')
    hub, messages = Hub(), []
    context = {'key': ('job', 'attempt'), 'job_id': '42'}
    working = tmp_path / 'working'
    working.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'durable-data'))
    expected = tmp_path / 'durable-data' / 'raven' / 'exports'
    if configured:
        expected = tmp_path / 'chosen-exports'
        monkeypatch.setenv('RAVEN_EXPORT_DIR', str(expected))
    app = SimpleNamespace(state_dir=str(tmp_path / 'ephemeral-state') if state_enabled else None,
        say=messages.append, fail=messages.append)
    monkeypatch.setattr(job_panels, '_worker', lambda *args: hub)
    monkeypatch.setattr(trace_ui, 'cancel', browser.cancel)
    assert browser.request(app, value, context)
    assert not expected.exists(), 'Directory creation belongs to the background task'
    result = hub.work()
    output = Path(result['path'])
    assert output.parent == expected
    assert output.is_file() and output.stat().st_mode & 0o777 == 0o600
    assert not (working / 'raven-exports').exists()
    assert not (tmp_path / 'ephemeral-state').exists()


@pytest.mark.parametrize('cancelled', [False, True])
def test_changed_selection_retains_export_feedback_without_opening_stale_attempt(tmp_path, monkeypatch, cancelled):
    from raven import job_panels, trace_ui
    value, _ = snapshot(tmp_path / 'trace')
    hub, messages, opened = Hub(), [], []
    context = {'key': ('job', 'attempt'), 'job_id': '42'}
    app = SimpleNamespace(state_dir=str(tmp_path / 'state'), say=messages.append, fail=messages.append)
    monkeypatch.setattr(job_panels, '_worker', lambda *args: hub)
    monkeypatch.setattr(trace_ui, 'cancel', browser.cancel)
    monkeypatch.setattr(trace_ui, '_context', lambda _: {'key': ('different', 'attempt')})
    monkeypatch.setattr(browser, 'open_browser', lambda path: opened.append(path))
    assert browser.request(app, value, context)
    result = hub.work()
    if cancelled:
        browser.cancel(app)
    hub.done(result)
    assert not opened
    assert app.trace_browser_state['path'] == result['path']
    assert any('previous selection' in message for message in messages)
    assert not app.trace_browser_state['pending']


@pytest.mark.parametrize("large_window", [False, True])
def test_managed_browser_action_uses_real_admitted_background_lane(tmp_path, monkeypatch, large_window):
    from raven import job_panels, trace_ui
    from raven.research import ResearchHub
    from raven.raven_execution import ExecutionRouter, background_allowed
    value, _ = snapshot(tmp_path / 'trace')
    context = {'key': ('job', 'attempt'), 'job_id': '42', 'light': True}
    config = {'managed': True, 'mode': 'light', 'site': 'desktop'}
    if large_window:
        root = tmp_path / 'large-trace'
        root.mkdir()
        with Recorder(root, job_id='42', attempt='exact-attempt', background_flush=False,
                      coverage={'adapter': 'python-profile', 'scope': 'synthetic large retained window'}) as recorder:
            for index in range(6500):
                recorder.emit('call', {'frame': str(index),
                    'function': 'calculate_reproducible_iteration_' + str(index) + '_science_phase_' * 4,
                    'file': '/project/experiments/model/training/iterations.py', 'line': 80}, thread='one')
                recorder.emit('return', {'frame': str(index)}, thread='one')
        value = read_trace(recorder.path)
        assert len(value['events']) == 13002 and value['caught_up']
    monkeypatch.setattr(trace_export.protocol, 'read_trace', lambda *args, **kwargs: pytest.fail('Export reread source instead of using the published window'))
    hub = ResearchHub({'raven': config})
    hub.execution = ExecutionRouter(config)
    messages, opened = [], []
    app = SimpleNamespace(state_dir=str(tmp_path / 'state'), research=hub, say=messages.append, fail=messages.append)
    monkeypatch.setattr(job_panels, '_worker', lambda *args: hub)
    monkeypatch.setattr(trace_ui, 'cancel', browser.cancel)
    monkeypatch.setattr(trace_ui, '_context', lambda _: context)
    monkeypatch.setattr(browser, 'open_browser', lambda path: opened.append(path) or 'Requested')
    try:
        assert not background_allowed(hub.execution, lambda: None)
        assert browser.request(app, value, context), messages
        hub.future.result(timeout=5)
        assert not opened
        hub.poll_task()
        assert len(opened) == 1
        output = Path(app.trace_browser_state['path'])
        assert output.exists() and output.stat().st_size <= browser.MAX_OUTPUT_BYTES
        if large_window:
            assert output.stat().st_size > 2 << 20
        assert document(output.read_bytes())['identity']['attempt'] == 'exact-attempt'
    finally:
        hub.close()


def test_cpu_observations_preserve_socket_core_hierarchy_and_query_uncertainty(tmp_path):
    value, _ = snapshot(tmp_path)
    for index, socket, core, cpu in [(1, 2, 7, 19), (2, 1, 3, 8)]:
        event = value['events'][index]
        event['data']['cpu_observation'] = {
            'schema': 'raven.cpu-placement/v1', 'logical_cpu': cpu, 'socket_id': socket,
            'core_id': core, 'sample_start_ns': event['mono_ns'] + 2,
            'sample_end_ns': event['mono_ns'] + 8, 'source': 'linux-sched_getcpu'}
    raw = browser.render(value)
    model = document(raw)
    points = model['cpu']['observations']
    assert model['cpu']['status'] == 'observed'
    assert [row['logical_cpu'] for row in points] == [19, 8]
    assert [row['lane_order'] for row in points] == [1, 0]
    assert points[0]['lane_label'] == 'Socket 2 / core 7 / logical CPU 19'
    assert points[0]['duration_ns'] == '6'
    assert points[0]['kind'] == 'placement'
    assert points[0]['first_seq'] == points[0]['last_seq'] == value['events'][1]['seq']
    assert points[0]['plot_at_ms'] == (int(points[0]['start_ns']) + 3) / 1e6
    assert b'Query brackets do not establish CPU residency between events' in raw
    assert b'CPU placement observations' in raw


def test_unrecorded_cpu_placement_never_synthesizes_execution_from_calls(tmp_path):
    value, _ = snapshot(tmp_path)
    model = document(browser.render(value))
    assert model['intervals']
    assert model['cpu']['status'] == 'not-recorded'
    assert not model['cpu']['observations'] and not model['cpu']['lanes']
    assert '--trace-cpu-placement' in ' '.join(model['cpu']['warnings'])
