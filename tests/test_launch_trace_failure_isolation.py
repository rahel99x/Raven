"""Optional trace failures must not change the actual Python application."""
import json
import os
from pathlib import Path
import sys

import pytest

from raven import launch_trace as T, reporting as R
from raven.tracing import capture as trace_recorder


@pytest.fixture
def application(tmp_path, monkeypatch):
    script = tmp_path / 'application.py'
    script.write_text('from pathlib import Path\nPath(__file__).with_name("ran").write_text("once")\n')
    run = R.begin_run(tmp_path, 'attempt', script='application.py', name='real workload')
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'project_root': str(tmp_path), 'settings': {'trace_adapter': 'python-legacy-profile'}}))
    monkeypatch.setenv('RAVEN_RUN_DIR', str(run))
    monkeypatch.setenv('RAVEN_LAUNCH_PLAN', str(plan))
    monkeypatch.setenv('RAVEN_RUNTIME_BUNDLE', str(tmp_path / 'runtime.pyz'))
    monkeypatch.setattr(sys, 'argv', ['-c', str(script), 'argument'])
    monkeypatch.setattr(sys, 'path', list(sys.path))
    return script, run


@pytest.mark.parametrize('failure_file', ['run.json', 'plan.json'])
def test_metadata_failure_still_executes_application_once(application, monkeypatch, failure_file, capsys):
    script, run = application
    original = R._read_json
    def failed_read(path):
        if Path(path).name == failure_file:
            raise OSError('metadata temporarily unavailable')
        return original(path)
    monkeypatch.setattr(R, '_read_json', failed_read)
    T.python_application()
    assert script.with_name('ran').read_text() == 'once'
    assert 'application executes normally' in capsys.readouterr().err


def _broken_capture(monkeypatch, run, failure):
    class Recorder:
        def __init__(self, *args, **kwargs):
            self.directory = run / 'captured'
            self.path = self.directory / 'manifest.json'
            self.stream, self.state, self.warnings = 'stream', 'OPEN', []
        def _fail(self, state, message):
            self.state = state
            self.warnings.append(message)
        def close(self):
            if failure == 'close':
                raise OSError('trace disk unavailable during close')
            if self.state == 'OPEN':
                self.state = 'CLOSED'
    class Capture:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            if failure == 'start-and-stop':
                raise ValueError('optional tracing unavailable')
        def stop(self):
            if failure in ('stop', 'start-and-stop'):
                raise OSError('trace shutdown failed')
    monkeypatch.setattr(trace_recorder, 'Recorder', Recorder)
    monkeypatch.setattr(trace_recorder, 'PythonCapture', Capture)
    monkeypatch.setattr(trace_recorder, 'register_trace', lambda *args: None)


@pytest.mark.parametrize('failure', ['stop', 'close', 'start-and-stop'])
@pytest.mark.parametrize('outcome', ['success', 'exit', 'exception'])
def test_cleanup_failure_preserves_success_exit_or_original_exception(application, monkeypatch, failure, outcome):
    script, run = application
    _broken_capture(monkeypatch, run, failure)
    if outcome == 'exit':
        script.write_text(script.read_text() + 'raise SystemExit(7)\n')
        with pytest.raises(SystemExit) as captured:
            T.python_application()
        assert captured.value.code == 7
    elif outcome == 'exception':
        script.write_text(script.read_text() + 'raise RuntimeError("application failure")\n')
        with pytest.raises(RuntimeError, match='^application failure$'):
            T.python_application()
    else:
        T.python_application()
    assert script.with_name('ran').read_text() == 'once'
    if failure == 'stop':
        index = R._read_json(run / 'trace/index.json')
        assert all(source['status'] == 'partial' for source in index['sources'])


def test_failed_warning_storage_does_not_prevent_application(application, monkeypatch, capsys):
    script, run = application
    from raven.launching import diagnostics as launch_runtime
    monkeypatch.setattr(R, '_read_json', lambda path: (_ for _ in ()).throw(OSError('metadata unavailable')))
    monkeypatch.setattr(launch_runtime, '_warn', lambda *args: (_ for _ in ()).throw(RuntimeError('warning storage unavailable')))
    T.python_application()
    assert script.with_name('ran').read_text() == 'once'
    assert 'Raven tracing:' in capsys.readouterr().err


def test_hook_metadata_failure_executes_original_argv_unchanged(application, monkeypatch):
    commands = []
    monkeypatch.setattr(R, '_read_json', lambda path: (_ for _ in ()).throw(OSError('missing plan')))
    def execute(program, argv, env):
        commands.append((program, argv))
        raise SystemExit(19)
    monkeypatch.setattr(os, 'execvpe', execute)
    with pytest.raises(SystemExit) as captured:
        T.hook(['python3', 'application.py', '', '--x', 'two words'])
    assert captured.value.code == 19
    assert commands == [('python3', ['python3', 'application.py', '', '--x', 'two words'])]


@pytest.mark.parametrize('metadata', [[], {'trace': True, 'settings': []}, {'trace': True, 'settings': {'trace_adapter': []}}, {'trace': 'yes'}, {'trace': True, 'settings': {'trace_adapter': 'native-cooperative'}}])
def test_invalid_or_unsupported_hook_metadata_keeps_original_command_despite_broken_warning_sink(application, monkeypatch, metadata):
    from raven.launching import diagnostics as launch_runtime
    monkeypatch.setattr(R, '_read_json', lambda path: metadata)
    monkeypatch.setattr(launch_runtime, '_warn', lambda *args: (_ for _ in ()).throw(ValueError('warning sink closed')))
    commands = []
    def execute(program, argv, env):
        commands.append((program, argv))
        raise SystemExit(23)
    monkeypatch.setattr(os, 'execvpe', execute)
    with pytest.raises(SystemExit) as captured:
        T.hook(['application', '--flag', ''])
    assert captured.value.code == 23
    assert commands == [('application', ['application', '--flag', ''])]


def test_closed_stderr_during_trace_shutdown_cannot_replace_application_exit(application, monkeypatch):
    import io
    script, run = application
    _broken_capture(monkeypatch, run, 'stop')
    script.write_text(script.read_text() + 'raise SystemExit(11)\n')
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, 'stderr', closed)
    with pytest.raises(SystemExit) as captured:
        T.python_application()
    assert captured.value.code == 11
    assert script.with_name('ran').read_text() == 'once'
