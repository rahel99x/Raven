from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime, timezone
import os
import threading
import time
from types import SimpleNamespace

import pytest

from tower import activity_ui, doctor, layout as L, session_tools
from tower.alerts import AlertEngine
from tower.config import Config
from tower.controller import App
from tower.logs import LogBuffer
from tower.model import Finished, Job, Store
from tower.remote import LocalFiles
from tower.research import ResearchHub


def app_for(tmp_path):
    events, saves = [], []
    snap = {'jobs': [], 'finished': [], 'live': {}, 'details': {}}
    def event(kind, text, **values):
        record = dict(kind=kind, text=text, job=values.pop('job_id', ''), **values)
        events.append(record)
        return record
    store = SimpleNamespace(snapshot=lambda: snap, event=event, alerts=None)
    app = SimpleNamespace(user='alex', cfg={'host': '', 'clipboard': {'osc52': False, 'tools': False}},
        profile_name='carc', mode='main', tab='jobs', state_dir=str(tmp_path / 'state'), store=store,
        logs=SimpleNamespace(files=LocalFiles()), research=ResearchHub({}, LocalFiles()), message='', command_ok=True,
        height=26, width=100, cursor={'history': 0}, filter='', table_state={'facets': {'history': {}}},
        log_job=None, log_record=None)
    app.save = lambda: saves.append(True)
    app.say = lambda text: setattr(app, 'message', text)
    def fail(text): app.message, app.command_ok = text, False
    app.fail = fail
    app.enter_tab = lambda tab: setattr(app, 'tab', tab)
    app.history_jobs = lambda: snap['finished']
    app.sync_history_selection = lambda: setattr(app, 'selected_id', snap['finished'][app.cursor['history']].id)
    app.open_log = lambda jid: setattr(app, 'log_job', jid)
    app.test_events, app.test_saves, app.test_snap = events, saves, snap
    activity_ui.initialize(app)
    session_tools.initialize(app)
    return app


def complete(app):
    try: app.research.future.result(timeout=5)
    except Exception: pass
    app.research.poll_task()


def controller_app_for(tmp_path):
    cfg = Config({'clipboard': {'osc52': False, 'tools': False}})
    app = App(Store(str(tmp_path / 'state')), None, None, cfg, 'alex', ascii_=True, interactive=False)
    app.logs.files = LocalFiles()
    app.research = ResearchHub(cfg, app.logs.files)
    return app


def test_full_log_copy_publishes_completed_tail_before_admission_and_keeps_exact_source(tmp_path):
    app = controller_app_for(tmp_path)
    p = tmp_path / 'exact source.log'
    raw = b'FIRST\toriginal\r\n' + ('界 Unicode\tdata\r\n' * 2000).encode() + b'LAST\tend'
    p.write_bytes(raw)
    other = tmp_path / 'other.log'
    other.write_bytes(b'WRONG SOURCE\n')
    app.tab, app.logs.path = 'log', str(p)
    buf = LogBuffer(str(p), max_bytes=32, files=app.logs.files)
    buf.refresh()
    app.logs.sync_buffer(buf)
    previous, published = Future(), []
    previous.set_result('cached tail')
    app.research.future, app.research.pending = previous, (previous, published.append)
    try:
        assert buf.truncated and buf.raw_lines != raw.splitlines()
        app.copy_all_log()
        assert published == ['cached tail'] and app.research.future is not previous
        assert app.command_ok and app.activity.task['source'] == str(p)
        complete(app)
        exports = list((tmp_path / 'state' / 'exports').glob('log-full-*.log'))
        assert len(exports) == 1 and exports[0].read_bytes() == raw
        assert app.activity.task['status'] == 'ready'
    finally:
        app.research.close()


def test_log_copy_does_not_wait_for_or_queue_behind_unfinished_work(tmp_path):
    app = controller_app_for(tmp_path)
    p = tmp_path / 'source.log'
    p.write_bytes(b'original log\n')
    app.tab, app.logs.path = 'log', str(p)
    previous, published = Future(), []
    pending = (previous, published.append)
    app.research.future, app.research.pending = previous, pending
    try:
        before = time.monotonic()
        app.copy_all_log()
        assert time.monotonic() - before < .2
        assert app.research.pending is pending and app.research.future is previous
        assert not published and not previous.done()
        assert not app.command_ok and 'background command is still running' in app.message
        assert app.activity.task is None and not list(tmp_path.rglob('log-full-*.log'))
    finally:
        previous.cancel()
        app.research.close()


@pytest.mark.parametrize('change', ['selection', 'source', 'backend', 'job',
    'run_id', 'attempt', 'project_root', 'run_root', 'path', 'workdir'])
def test_log_copy_rejects_source_or_selection_changed_by_completed_publication(tmp_path, change):
    app = controller_app_for(tmp_path)
    p = tmp_path / 'source.log'
    p.write_bytes(b'pinned old line\nsecond line\n')
    app.tab, app.logs.path = 'log', str(p)
    buf = LogBuffer(str(p), files=app.logs.files)
    buf.refresh()
    app.logs.sync_buffer(buf)
    if change == 'selection':
        app.logs.begin_selection(buf, 0)
    binding_keys = ('run_id', 'attempt', 'project_root', 'run_root', 'path', 'workdir')
    if change in binding_keys:
        app.project_state['binding'] = {'run_id': 'run-1', 'attempt': 1,
            'project_root': '/project', 'run_root': '/project/runs/run-1',
            'path': '/project/run.json', 'workdir': '/work'}
        assert app.log_job is None and app.log_record is None
    previous = Future()
    previous.set_result('changed')
    def publish(_value):
        if change == 'selection':
            buf.reloads += 1
            app.logs.sync_buffer(buf)
        elif change == 'source':
            app.logs.path = str(tmp_path / 'other.log')
        elif change == 'backend':
            app.logs.files = LocalFiles()
        elif change in binding_keys:
            app.project_state['binding'][change] = 2 if change == 'attempt' else 'changed-' + change
        else:
            app.log_job = 'another-job'
    app.research.future, app.research.pending = previous, (previous, publish)
    try:
        app.copy_selected_log(buf) if change == 'selection' else app.copy_all_log()
        assert app.research.pending is None and app.research.future is previous
        assert not app.command_ok and 'changed' in app.message.lower()
        assert app.activity.task is None and not list(tmp_path.rglob('log-*.log'))
    finally:
        app.research.close()


def test_report_export_publishes_completed_work_before_capturing_snapshot(tmp_path):
    from tower.views import Views
    app = controller_app_for(tmp_path)
    app.views_ref = Views(L.Glyphs(True), app.cfg, files=app.logs.files)
    app.store.apply_jobs([Job('7', 'before publication', 'main', 'RUNNING')])
    previous = Future()
    previous.set_result('captured report job')
    def publish(name):
        app.store.job('7').name = name
    app.research.future, app.research.pending = previous, (previous, publish)
    try:
        app.export_report_background()
        assert app.command_ok and app.research.future is not previous
        complete(app)
        exports = list((tmp_path / 'state' / 'exports').glob('report-*.txt'))
        assert len(exports) == 1
        text = exports[0].read_text()
        assert 'captured report job' in text and 'before publication' not in text
        assert text.endswith('End of report | Raven | Plain ASCII, no external resources.\n')
        assert app.activity.task['status'] == 'ready'
    finally:
        app.research.close()


def test_report_export_does_not_wait_for_or_prepare_behind_unfinished_work(tmp_path, monkeypatch):
    from tower import report
    app = controller_app_for(tmp_path)
    previous, published = Future(), []
    pending = (previous, published.append)
    app.research.future, app.research.pending = previous, pending
    monkeypatch.setattr(report, 'prepare_export', lambda *_args: pytest.fail('busy report prepared a snapshot'))
    try:
        before = time.monotonic()
        app.export_report_background()
        assert time.monotonic() - before < .2
        assert app.research.pending is pending and app.research.future is previous
        assert not published and not previous.done()
        assert not app.command_ok and 'background command is still running' in app.message
        assert app.activity.task is None and not list(tmp_path.rglob('report-*.txt'))
    finally:
        previous.cancel()
        app.research.close()


def finished(jid, state='COMPLETED', end='2026-10-05T10:00:00'):
    return Finished(jid, name='job-' + jid, state=state, start='2026-10-05T09:00:00', end=end, partition='gpu')


def test_completion_inbox_uses_accounting_and_exact_reordered_identity(tmp_path):
    app = app_for(tmp_path)
    try:
        first, second = finished('8'), finished('9', 'FAILED')
        app.test_snap['finished'] = [first, second]
        session_tools.run_command(app, ['inbox', 'all'])
        session_tools.overlay(SimpleNamespace(g=L.Glyphs(False)), app.test_snap, app, 100, 26)
        session_tools.handle_key(app, 'down')
        selected = app.session_tools_state['selected']
        assert session_tools._pick(app)['record'].id == '8'
        app.test_snap['finished'].insert(0, finished('10', 'OUT_OF_MEMORY', '2026-10-05T11:00:00'))
        session_tools.observe(app, app.test_snap)
        session_tools.handle_key(app, 'r')
        assert selected in app.session_tools_state['reviewed']
        assert len(app.session_tools_state['reviewed']) == 1
        session_tools.observe(app, {'jobs': [], 'finished': [Finished('pending', state='RUNNING')]})
        assert all(item['record'].id != 'pending' for item in session_tools.inbox_items(app))
    finally: app.research.close()


def test_inbox_scoped_restart_and_job_id_reuse_are_distinct(tmp_path):
    app = app_for(tmp_path)
    try:
        first = finished('123')
        newer = finished('123', end='2026-10-06T10:00:00')
        app.test_snap['finished'] = [first, newer]
        session_tools.observe(app, app.test_snap)
        assert len(app.session_tools_state['records']) == 2
        session_tools._ack(app, [session_tools._identity(first)])
        saved = session_tools.save(app)
        session_tools.initialize(app)['reviewed'] = []
        session_tools.restore(app, saved)
        assert session_tools.unread_count(app) == 1
        app.profile_name = 'other'
        app.session_tools_state['reviewed'] = []
        session_tools.restore(app, saved)
        assert not app.session_tools_state['reviewed']
        assert session_tools._identity(finished('123', 'FAILED+')) == session_tools._identity(finished('123', 'FAILED'))
    finally: app.research.close()


def test_history_open_anchors_exact_record_and_failed_open_is_not_reviewed(tmp_path):
    app = app_for(tmp_path)
    try:
        old, new = finished('123'), finished('123', end='2026-10-06T10:00:00')
        app.test_snap['finished'] = [old, new]
        session_tools.observe(app, app.test_snap)
        item = {'key': session_tools._identity(new), 'record': new}
        session_tools._open_completion(app, item)
        assert app.cursor['history'] == 1 and item['key'] in app.session_tools_state['reviewed']
        absent = finished('not-in-history')
        session_tools._open_completion(app, {'key': session_tools._identity(absent), 'record': absent})
        assert app.mode == 'session_inbox'
        assert session_tools._identity(absent) not in app.session_tools_state['reviewed']
    finally: app.research.close()


def test_completed_log_open_rejects_reused_active_job_id(tmp_path):
    app = app_for(tmp_path)
    try:
        old = finished('123')
        app.test_snap['finished'] = [old]
        app.test_snap['jobs'] = [Job('123', name='new job', partition='gpu', state='RUNNING', user='alex', start='2026-10-06T10:00:00')]
        session_tools._open_completion(app, {'key': session_tools._identity(old), 'record': old}, logs=True)
        assert app.mode == 'session_inbox' and app.log_job is None
        assert 'another or unverified execution' in app.message.lower()
        assert not app.session_tools_state['reviewed']
    finally: app.research.close()


def test_completion_bounds_and_invalid_commands(tmp_path):
    app = app_for(tmp_path)
    try:
        for base in (0, 200, 400):
            session_tools.observe(app, {'finished': [finished(str(base + n), end=f'2026-10-{n % 20 + 1:02}T10:00:00') for n in range(200)]})
        assert len(app.session_tools_state['records']) == 256
        session_tools.run_command(app, ['inbox', 'unread', 'extra'])
        assert not app.command_ok
    finally: app.research.close()


def test_activity_export_records_are_bounded_detached_and_restart_safe(tmp_path):
    app = app_for(tmp_path)
    try:
        for n in range(200):
            app.activity.post(f'job message {n}', level='warning', path=str(tmp_path / f'{n}.log'), job=str(n), task='report')
        assert len(app.activity.exports) == 128 and len(app.activity.notices) == 200
        notices, _ = app.activity.snapshot()
        notices[0]['text'] = 'mutated'
        assert app.activity.notices[0]['text'] != 'mutated'
        saved = activity_ui.save(app)
        assert saved['exports'] and not saved['notices']
        saved['exports'][0]['label'] = 'mutated'
        assert app.activity.exports[0]['label'] != 'mutated'
        app.activity = activity_ui.Activity()
        activity_ui.restore(app, saved)
        assert len(app.activity.exports) == 128 and not app.activity.notices
        activity_ui.run_command(app, ['activity', 'persistent', 'on'])
        app.activity.post('saved notice', job='7')
        notices_saved = activity_ui.save(app)
        assert len(notices_saved['notices']) == 1
        activity_ui.run_command(app, ['activity', 'clear'])
        assert not app.activity.notices and app.test_saves
    finally: app.research.close()


def test_activity_filters_labels_and_forget_do_not_delete_files(tmp_path):
    app = app_for(tmp_path)
    p = tmp_path / 'report.txt'
    p.write_text('contents\n')
    try:
        app.activity.post('one', level='error', path=str(p), job='123', task='export')
        app.activity.post('two', level='info', job='456', task='search')
        activity_ui.run_command(app, ['activity', 'filter', 'level=error', 'job=123', 'task=export'])
        assert len(activity_ui._ordered(app)) == 1
        activity_ui.run_command(app, ['exports', 'label', '1', 'failure report'])
        assert activity_ui.export_items(app)[0]['label'] == 'failure report'
        activity_ui.run_command(app, ['exports', 'forget', '1'])
        assert not activity_ui.export_items(app) and p.read_text() == 'contents\n'
    finally: app.research.close()


def test_export_preview_is_worker_bounded_and_survives_close(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    p = tmp_path / 'report.txt'
    p.write_text('界 line\n' * 10000)
    original_open, started, release = os.open, threading.Event(), threading.Event()
    def delayed(path, flags, *args, **kwargs):
        if str(path) == str(p):
            assert threading.current_thread().name.startswith('tower-research')
            started.set()
            release.wait(timeout=3)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', delayed)
    try:
        app.activity.post('export', path=str(p))
        activity_ui.run_command(app, ['exports', 'preview', '1'])
        assert started.wait(timeout=3)
        activity_ui.run_command(app, ['exports', 'show'])
        release.set()
        complete(app)
        assert app.mode == 'exports' and app.activity.export_preview is None
        activity_ui.run_command(app, ['exports', 'preview', '1'])
        complete(app)
        assert len(app.activity.export_preview['lines']) <= 257
        assert 'Preview limit reached' in app.activity.export_preview['lines'][-1]
    finally:
        release.set()
        app.research.close()


def test_export_preview_publishes_completed_pending_work_before_admission(tmp_path):
    app = app_for(tmp_path)
    p = tmp_path / 'report.txt'
    p.write_text('exact export contents\n')
    previous, published = Future(), []
    previous.set_result('cached tail')
    app.research.future = previous
    app.research.pending = (previous, published.append)
    try:
        app.activity.post('export', path=str(p))
        activity_ui.run_command(app, ['exports', 'preview', '1'])
        assert published == ['cached tail']
        assert app.mode == 'export_preview' and app.command_ok
        assert app.research.future is not previous
        assert app.research.pending[0] is app.research.future
        complete(app)
        assert app.activity.export_preview['lines'] == ['exact export contents']
    finally:
        app.research.close()


def test_export_preview_does_not_wait_for_or_queue_behind_unfinished_work(tmp_path):
    app = app_for(tmp_path)
    p = tmp_path / 'report.txt'
    p.write_text('export contents\n')
    previous, published = Future(), []
    pending = (previous, published.append)
    app.research.future, app.research.pending = previous, pending
    try:
        app.activity.post('export', path=str(p))
        activity_ui.run_command(app, ['exports', 'show'])
        before = time.monotonic()
        activity_ui.run_command(app, ['exports', 'preview', '1'])
        assert time.monotonic() - before < .2
        assert app.research.pending is pending and app.research.future is previous
        assert not published and not previous.done()
        assert app.mode == 'exports' and app.activity.export_preview is None
        assert not app.command_ok and 'background operation is running' in app.message
    finally:
        previous.cancel()
        app.research.close()


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'directory'])
def test_export_preview_rejects_nonregular_or_symlink_sources(tmp_path, kind):
    app = app_for(tmp_path)
    p = tmp_path / 'source'
    if kind == 'symlink':
        real = tmp_path / 'real'
        real.write_text('secret\n')
        p.symlink_to(real)
    elif kind == 'fifo': os.mkfifo(p)
    else: p.mkdir()
    try:
        app.activity.post('export', path=str(p))
        activity_ui.run_command(app, ['exports', 'preview', '1'])
        complete(app)
        assert app.activity.export_preview['lines'] != ['secret']
        assert 'Loading' not in app.activity.export_preview['lines'][0]
    finally: app.research.close()


def test_snooze_expiry_prunes_cap_and_delivery_only_is_muted(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    now = [1000.0]
    monkeypatch.setattr('tower.alerts.time.time', lambda: now[0])
    notifications = []
    engine = AlertEngine([{'name': 'running', 'when': 'running', 'actions': ['bell', 'event', 'notify'], 'every': 1}], app.store, user='alex', notify=notifications.append)
    app.store.alerts = engine
    try:
        for n in range(256): engine.snooze('running', 1, str(n))
        now[0] += 2
        engine.snooze('running', 30, '123')
        assert len(engine.snoozes) == 1
        app.test_snap['jobs'] = [Job('123', name='job', partition='gpu', user='alex', state='RUNNING')]
        fired = engine.check(app.test_snap)
        assert fired and engine.rules[0].active == {'123'}
        assert app.test_events[-1]['kind'] == 'alert' and not notifications and engine.bell == 0
        now[0] += 31
        engine.check(app.test_snap)
        assert len(notifications) == 1 and engine.bell == 1
        assert not engine.controls_snapshot()['snoozes']
    finally: app.research.close()


@pytest.mark.parametrize('time_string, muted', [('2026-10-05T04:59:00+00:00', False), ('2026-10-05T05:00:00+00:00', True), ('2026-10-05T13:59:00+00:00', True), ('2026-10-05T14:00:00+00:00', False)])
def test_quiet_hours_cross_midnight_use_declared_zone(tmp_path, time_string, muted):
    app = app_for(tmp_path)
    engine = AlertEngine([{'name': 'running', 'when': 'running'}], app.store)
    try:
        engine.set_quiet('22:00', '07:00', 'America/Los_Angeles')
        timestamp = datetime.fromisoformat(time_string).timestamp()
        assert engine.notification_muted('running', now=timestamp) is muted
    finally: app.research.close()


def test_alert_restore_is_bounded_and_invalid_controls_atomic(tmp_path):
    app = app_for(tmp_path)
    engine = AlertEngine([{'name': 'running', 'when': 'running'}], app.store)
    try:
        engine.set_quiet('22:00', '07:00', 'America/Los_Angeles')
        original = engine.controls_snapshot()
        with pytest.raises((ValueError, KeyError)): engine.set_quiet('99:00', '07:00')
        assert engine.controls_snapshot()['quiet'] == original['quiet']
        with pytest.raises(ValueError): engine.snooze('running', float('nan'))
        engine.restore_controls({'quiet': None, 'snoozes': [{'rule': 'unknown', 'job': '*', 'until': float('inf')}]})
        assert engine.quiet is None and not engine.snoozes
    finally: app.research.close()


def test_terminal_diagnostics_are_passive_and_probe_never_actions(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    def forbidden(*args, **kwargs): raise AssertionError('scheduler or SSH invoked')
    monkeypatch.setattr('tower.remote.SshBackend.run', forbidden)
    monkeypatch.setattr('subprocess.run', forbidden)
    try:
        checks = doctor.terminal_evidence(app.cfg, state_dir=str(tmp_path), environ={'TERM': 'xterm-256color', 'TMUX': '1', 'SSH_TTY': '/dev/pts/1'}, encoding='ascii')
        assert any(c['name'] == 'Output encoding' and c['status'] == 'warning' for c in checks)
        assert any(c['name'] == 'tmux clipboard' for c in checks)
        session_tools.run_command(app, ['terminaldoctor'])
        complete(app)
        assert app.mode == 'terminal_diagnostics'
        session_tools.run_command(app, ['terminaltest'])
        for key in (':', 'c', 'q', '1', 'enter', 'ctrl-c'):
            assert session_tools.handle_key(app, key) and app.mode == 'terminal_probe'
        assert session_tools.handle_mouse(app, 3, 12, button='double')
        assert any('Mouse:' in e for e in app.session_tools_state['probe_events'])
        session_tools.handle_key(app, 'esc')
        assert app.mode == 'main'
    finally: app.research.close()


@pytest.mark.parametrize('mode', ['session_inbox', 'session_alerts', 'terminal_diagnostics', 'terminal_probe', 'activity', 'exports', 'export_preview'])
@pytest.mark.parametrize('ascii_', [True, False])
@pytest.mark.parametrize('size', [(1, 1), (3, 2), (8, 5), (80, 24), (160, 50)])
def test_operation_overlays_fit_and_use_ascii_fallback(tmp_path, mode, ascii_, size):
    app = app_for(tmp_path)
    try:
        app.test_snap['finished'] = [finished('123', 'FAILED')]
        app.store.alerts = AlertEngine([{'name': 'idle 界', 'when': 'running'}], app.store)
        app.activity.post('界 error', level='error', path=str(tmp_path / 'file'), job='123')
        app.activity.export_preview = {'label': '界 preview', 'path': '/tmp/file', 'lines': ['界 line'], 'scroll': 0}
        app.session_tools_state['diagnostics'] = doctor.terminal_evidence(app.cfg, environ={'TERM': 'dumb'}, encoding='ascii')
        app.mode = mode
        width, height = size
        views = SimpleNamespace(g=L.Glyphs(ascii_))
        rows = session_tools.overlay(views, app.test_snap, app, width, height) if mode.startswith(('session_', 'terminal_')) else activity_ui.overlay(views, app.test_snap, app, width, height)
        assert all(0 <= y < height and 0 <= x < width and x + L.vlen(L.row_text(row)) <= width for y, x, row in rows)
        if ascii_: assert all(L.row_text(row).isascii() for y, x, row in rows)
    finally: app.research.close()


def test_export_preview_rejects_same_inode_edits(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    p = tmp_path / 'report.txt'
    p.write_text('old text\n')
    real_stat = os.stat
    edited = []
    def change_before_publication(path, *args, **kwargs):
        if str(path) == str(p) and not edited:
            edited.append(True)
            p.write_text('new text\n')
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', change_before_publication)
    try:
        app.activity.post('export', path=str(p))
        activity_ui.run_command(app, ['exports', 'preview', '1'])
        complete(app)
        assert 'changed during the preview' in app.activity.export_preview['lines'][0]
    finally: app.research.close()


def test_malformed_restored_activity_times_do_not_crash_renderer(tmp_path):
    app = app_for(tmp_path)
    try:
        activity_ui.restore(app, {'persistent': True, 'exports': [{'path': str(tmp_path / 'x'), 'time': 1e300}],
            'notices': [{'text': 'bad', 'time': 1e300}, {'text': 'bool', 'time': True}]})
        app.mode = 'activity'
        rows = activity_ui.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, 100, 26)
        assert rows and all(item['time'] == 0 for item in app.activity.notices)
    finally: app.research.close()


def test_cached_older_completion_cannot_open_newer_finished_same_job_id(tmp_path):
    app = app_for(tmp_path)
    old = finished('9', 'FAILED', '2026-10-04T10:00:00')
    old.start, old.workdir = '2026-10-04T09:00:00', '/tmp/old'
    new = finished('9', 'COMPLETED', '2026-10-05T10:00:00')
    new.workdir = '/tmp/new'
    try:
        session_tools.observe(app, {'finished': [old]})
        app.test_snap['finished'] = [new]
        session_tools.observe(app, app.test_snap)
        app.job_record = lambda jid: new
        session_tools._open_completion(app, {'key': session_tools._identity(old), 'record': old}, logs=True)
        assert app.mode == 'session_inbox' and app.log_job is None
        assert not app.session_tools_state['reviewed']
        assert 'cached completion remains unread' in app.message
        session_tools._open_completion(app, {'key': session_tools._identity(old), 'record': old})
        assert session_tools._identity(old) not in app.session_tools_state['reviewed']
        assert 'filters or date window' in app.message
    finally: app.research.close()


def test_export_selection_stays_attached_when_new_exports_arrive(tmp_path):
    app = app_for(tmp_path)
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.write_text('first content\n')
    second.write_text('second content\n')
    try:
        app.activity.post('first export', path=str(first))
        activity_ui.run_command(app, ['exports'])
        activity_ui.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, 100, 26)
        assert app.activity.export_selected == app.activity.exports[0]['id']
        app.activity.post('second export', path=str(second))
        activity_ui.handle_key(app, 'enter')
        complete(app)
        assert app.activity.export_preview['path'] == str(first)
        assert app.activity.export_preview['lines'] == ['first content']
    finally: app.research.close()


def test_inbox_and_exports_mouse_hits_retain_exact_identity_after_reorder(tmp_path):
    app = app_for(tmp_path)
    try:
        old = finished('1', 'FAILED')
        app.test_snap['finished'] = [old]
        session_tools.run_command(app, ['inbox'])
        session_tools.overlay(SimpleNamespace(g=L.Glyphs(False)), app.test_snap, app, 100, 26)
        y, hit = next(iter(app.session_tools_state['mouse_rows'].items()))
        app.test_snap['finished'].append(finished('2', 'FAILED', '2026-10-06T10:00:00'))
        session_tools.observe(app, app.test_snap)
        assert session_tools.handle_mouse(app, y, hit[1])
        assert session_tools._pick(app)['record'].id == '1'
        p = tmp_path / 'one'
        p.write_text('one\n')
        app.activity.post('first', path=str(p))
        activity_ui.run_command(app, ['exports'])
        activity_ui.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, 100, 26)
        y, hit = next(iter(app.activity.mouse_rows.items()))
        other = tmp_path / 'two'
        other.write_text('two\n')
        app.activity.post('second', path=str(other))
        assert activity_ui.handle_mouse(app, y, hit[1], button='double')
        complete(app)
        assert app.activity.export_preview['path'] == str(p)
    finally: app.research.close()


def test_alert_condition_clears_when_its_job_leaves_published_queue(tmp_path):
    app = app_for(tmp_path)
    engine = AlertEngine([{'name': 'running', 'when': 'running', 'actions': ['event']}], app.store)
    try:
        app.test_snap['jobs'] = [Job('123', name='job', partition='gpu', user='alex', state='RUNNING')]
        engine.check(app.test_snap)
        assert engine.active_count() == 1
        app.test_snap['jobs'] = []
        engine.check(app.test_snap)
        assert engine.active_count() == 0 and engine.active_text() == []
        assert any(event['kind'] == 'alert' for event in app.test_events)
    finally: app.research.close()
