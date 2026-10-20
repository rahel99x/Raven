from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from raven import layout as L, log_tools as tools
from raven.activity_ui import Activity
from raven.logs import LogSession
from raven.remote import LocalFiles
from raven.research import ResearchHub

@pytest.fixture(autouse=True)
def durable_export_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(tmp_path / "state" / "exports"))



def app_for(tmp_path, files=None):
    files = files or LocalFiles()
    app = SimpleNamespace(logs=LogSession(max_bytes=2048, files=files), research=ResearchHub({}, files),
                          activity=Activity(), cfg={'clipboard': {'osc52': False, 'tools': False}},
                          log_job='77', project_state={}, mode='main', tab='log', width=100, height=26,
                          state_dir=str(tmp_path / 'state'), message='', command_ok=True)
    app.resolve_log_path = lambda: app.logs.entry['path'] if app.logs.entry else app.logs.path
    app.say = lambda value: setattr(app, 'message', value)
    def fail(value):
        app.message, app.command_ok = value, False
    app.fail = fail
    tools.initialize(app)
    return app


def complete(app):
    try:
        app.research.future.result(timeout=5)
    except Exception:
        pass
    app.research.poll_task()


def text(rows):
    return '\n'.join(L.row_text(row) for y, x, row in rows)


def open_file(app, path):
    app.logs.entry = {'id': 'out', 'label': 'stdout', 'path': str(path), 'job_id': app.log_job}
    app.logs.path = str(path)
    buf = app.logs.buffer(str(path))
    tools.observe_buffer(app, buf)
    return buf


def test_full_search_results_open_exact_file_then_return_to_results_and_tail(tmp_path):
    out, err = tmp_path / '77.out', tmp_path / '77.err'
    out.write_bytes(b'FAILED first\n' + b'x' * 5000 + b'\nlast\n')
    err.write_text('before\nFAILED stderr\nafter\n')
    app = app_for(tmp_path)
    try:
        open_file(app, out)
        app.logs.entries = [dict(app.logs.entry), {'id': 'err', 'label': 'stderr', 'path': str(err), 'job_id': '77'}]
        original = app.logs.path, app.logs.top, app.logs._buffer_token
        assert tools.run_command(app, ['logsearch', '--all', 'FAILED'])
        complete(app)
        matches = app.log_tools_state['results']['matches']
        assert [m['source']['path'] for m in matches] == [str(out), str(err)]
        tools.handle_key(app, 'down')
        tools.handle_key(app, 'enter')
        complete(app)
        assert app.mode == 'log_tools_page'
        assert app.log_tools_state['page']['path'] == str(err)
        assert 'FAILED stderr' in text(tools.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, 100, 26))
        assert (app.logs.path, app.logs.top, app.logs._buffer_token) == original
        tools.handle_key(app, 'esc')
        assert app.mode == 'log_tools_results' and app.log_tools_state['result_cursor'] == 1
        tools.handle_key(app, 'esc')
        assert app.mode == 'main' and app.logs.path == str(out)
    finally: app.research.close()


def test_old_page_selection_and_copy_all_use_own_source_not_tail(tmp_path):
    out, err = tmp_path / '77.out', tmp_path / '77.err'
    out.write_bytes(b'wrong source\n')
    raw = b'first\r\n\xffsecond\npartial'
    err.write_bytes(raw)
    app = app_for(tmp_path)
    try:
        open_file(app, out)
        tools._show_page(app, {'id': 'err', 'path': str(err), 'label': 'stderr'})
        complete(app)
        tools.handle_key(app, 'v')
        tools.handle_key(app, 'down')
        assert app.log_tools_state['selection'] == (0, 1)
        tools.handle_key(app, 'y')
        complete(app)
        selected = list((tmp_path / 'state' / 'exports').glob('log-selected-*.log'))
        assert len(selected) == 1 and selected[0].read_bytes() == b'first\r\n\xffsecond\n'
        assert tools.run_command(app, ['copy', 'all'])
        complete(app)
        exported = list((tmp_path / 'state' / 'exports').glob('log-full-*.log'))
        assert len(exported) == 1 and exported[0].read_bytes() == raw
        assert app.logs.path == str(out)
    finally: app.research.close()


def test_named_page_mark_persists_and_rejects_file_replacement(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'first\nsecond\nthird\n')
    app = app_for(tmp_path)
    try:
        open_file(app, p)
        tools.run_command(app, ['loggoto', 'line', '2'])
        complete(app)
        assert tools.run_command(app, ['logmark', 'failure', 'begins'])
        marks = tools.save(app)
        json.dumps(marks)
        assert marks['marks'][0]['offset'] == 6
        assert marks['marks'][0]['line'] == 2
        app.log_tools_state['marks'] = []
        tools.restore(app, marks)
        tools.run_command(app, ['logmarks'])
        replacement = tmp_path / 'new'
        replacement.write_bytes(b'other\n')
        replacement.replace(p)
        tools.handle_key(app, 'enter')
        complete(app)
        assert app.mode == 'log_tools_marks'
        assert 'replaced' in app.message.lower()
        tools.handle_key(app, 'd')
        assert not app.log_tools_state['marks']
    finally: app.research.close()


def test_per_file_reading_positions_follow_and_replacement_checks(tmp_path):
    a, b = tmp_path / 'a.out', tmp_path / 'b.out'
    a.write_bytes(b'one\ntwo\nthree\nfour\n')
    b.write_bytes(b'other\n')
    app = app_for(tmp_path)
    try:
        buf = open_file(app, a)
        app.logs.top, app.logs.cursor = 0, 1
        tools.before_source_change(app)
        app.logs.path = ''
        open_file(app, b)
        assert app.logs.following
        tools.before_source_change(app)
        app.logs.path = ''
        open_file(app, a)
        assert app.logs.top == 0 and app.logs.cursor == 1
        tools.before_source_change(app)
        app.logs.path = ''
        open_file(app, b)
        tools.before_source_change(app)
        replacement = tmp_path / 'new'
        replacement.write_bytes(b'changed\n')
        replacement.replace(a)
        app.logs.path = ''
        open_file(app, a)
        assert app.logs.following and app.logs.cursor is None
    finally: app.research.close()


def test_retained_search_modes_apply_to_find_and_next_previous(tmp_path):
    p = tmp_path / 'job.out'
    p.write_text('ERROR\nerror\nerrors\nerror\n')
    app = app_for(tmp_path)
    try:
        buf = open_file(app, p)
        app.logs.search = 'error'
        assert tools.find_retained(app, buf) == (True, 0)
        app.logs.match, app.logs.cursor = None, None
        tools.run_command(app, ['logsearchmode', 'literal', 'case', 'word'])
        assert tools.find_retained(app, buf) == (True, 1)
        assert tools.find_retained(app, buf) == (True, 3)
        assert tools.find_retained(app, buf, backwards=True) == (True, 1)
        assert tools.find_retained(app, buf, backwards=True) == (True, 3)
    finally: app.research.close()


def test_scan_and_page_io_run_on_shared_worker_never_overlay(tmp_path):
    p = tmp_path / 'job.out'
    p.write_text('error\n')
    class Files(LocalFiles):
        def __init__(self): self.threads = []
        def snapshot_stat(self, path):
            self.threads.append(threading.current_thread().name)
            return super().snapshot_stat(path)
        def read(self, path, offset, length):
            self.threads.append(threading.current_thread().name)
            return super().read(path, offset, length)
    files = Files()
    app = app_for(tmp_path, files)
    try:
        app.logs.path = str(p)
        tools.run_command(app, ['logsearch', 'error'])
        complete(app)
        count = len(files.threads)
        for _ in range(30): tools.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, 100, 26)
        assert len(files.threads) == count
        assert all(name.startswith('raven-research') for name in files.threads)
    finally: app.research.close()


@pytest.mark.parametrize('size', [(1, 1), (3, 2), (8, 5), (40, 12), (100, 26), (160, 50)])
@pytest.mark.parametrize('ascii_', [True, False])
@pytest.mark.parametrize('mode', ['page', 'results', 'marks'])
def test_overlays_fit_and_ascii_fallback(tmp_path, size, ascii_, mode):
    p = tmp_path / 'job.out'
    p.write_text('界 error\nnext\n')
    app = app_for(tmp_path)
    try:
        open_file(app, p)
        if mode == 'page':
            tools.run_command(app, ['logolder'])
            complete(app)
            tools.handle_key(app, 'v')
        elif mode == 'results':
            tools.run_command(app, ['logsearch', 'error'])
            complete(app)
        else:
            tools.run_command(app, ['logmark', '界 failure'])
            tools.run_command(app, ['logmarks'])
        width, height = size
        rows = tools.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), {}, app, width, height)
        assert all(0 <= y < height and 0 <= x < width and x + L.vlen(L.row_text(row)) <= width for y, x, row in rows)
        if ascii_: assert text(rows).isascii()
    finally: app.research.close()


def test_mouse_selection_matches_visible_logical_rows(tmp_path):
    p = tmp_path / 'job.out'
    p.write_text('one\ntwo\nthree\n')
    app = app_for(tmp_path)
    try:
        open_file(app, p)
        tools.run_command(app, ['logolder'])
        complete(app)
        tools.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, 100, 26)
        hits = app.log_tools_state['mouse_rows']
        y = next(y for y, hit in hits.items() if hit[0] == 1)
        x = hits[y][1]
        assert tools.handle_mouse(app, y, x)
        assert app.log_tools_state['page_cursor'] == 1
        y2 = next(y for y, hit in hits.items() if hit[0] == 2)
        assert tools.handle_mouse(app, y2, x, shift=True)
        assert app.log_tools_state['selection'] == (1, 2)
    finally: app.research.close()


@pytest.mark.parametrize('command', [['loggoto', 'percent', 'NaN'], ['loggoto', 'line', '0'], ['logolder', '-1'], ['logsearch', '--bad', 'x'], ['logsearch', '--regex', '(a+)+'], ['logmark', ''], ['logsearchmode', 'bad']])
def test_invalid_commands_are_atomic_and_do_not_start_io(tmp_path, command):
    app = app_for(tmp_path)
    try:
        assert tools.run_command(app, command)
        assert not app.command_ok and app.research.pending is None
        assert app.mode == 'main' and app.log_tools_state['page'] is None
    finally: app.research.close()


def test_forward_pages_preserve_long_line_bytes_and_absolute_line_numbers(tmp_path):
    from raven import log_scan
    p = tmp_path / 'job.out'
    raw = b'a' * (log_scan.PAGE_BYTES * 2 + 71) + b'\nlast\n'
    p.write_bytes(raw)
    app = app_for(tmp_path)
    try:
        open_file(app, p)
        tools.run_command(app, ['logolder', '0'])
        complete(app)
        chunks = []
        first_page = app.log_tools_state['page']
        while True:
            page = app.log_tools_state['page']
            chunks.extend(row['raw'] for row in page['rows'])
            if page['end'] == page['size']: break
            tools.handle_key(app, ']')
            complete(app)
        assert b''.join(chunks) == raw
        assert app.log_tools_state['page']['rows'][-1]['line'] == 2
        tools.handle_key(app, '[')
        complete(app)
        tools.handle_key(app, '[')
        complete(app)
        assert app.log_tools_state['page']['start'] == first_page['start']
    finally: app.research.close()


def test_cancellation_publishes_no_results_and_keeps_source_context(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'a\n' * 100000)
    blocker, release = threading.Event(), threading.Event()
    class Slow(LocalFiles):
        def read(self, path, offset, length):
            blocker.set()
            release.wait(timeout=3)
            return super().read(path, offset, length)
    app = app_for(tmp_path, Slow())
    try:
        app.logs.path = str(p)
        tools.run_command(app, ['logsearch', 'absent'])
        assert blocker.wait(timeout=3)
        _, task = app.activity.snapshot()
        task['cancel'].set()
        release.set()
        complete(app)
        assert app.log_tools_state['results'] is None
        assert app.mode == 'main' and app.logs.path == str(p)
        assert 'cancelled' in app.message.lower()
    finally:
        release.set()
        app.research.close()


def test_exact_source_search_result_fragment_opens_matching_fragment(tmp_path):
    from raven import log_scan
    p = tmp_path / 'job.out'
    p.write_bytes(b'x' * log_scan.MAX_LINE_BYTES + b'FAILED fragment\nlast\n')
    app = app_for(tmp_path)
    try:
        open_file(app, p)
        tools.run_command(app, ['logsearch', 'FAILED'])
        complete(app)
        tools.handle_key(app, 'enter')
        complete(app)
        page = app.log_tools_state['page']
        assert 'FAILED fragment' in page['rows'][0]['text']
        assert page['rows'][0]['partial_start']
    finally: app.research.close()


def test_retained_pathological_regex_cannot_scan_oversized_tail_line(tmp_path):
    import time
    p = tmp_path / 'job.out'
    p.write_bytes(b'a' * 1000000 + b'\nshort b\n')
    app = app_for(tmp_path)
    app.logs.max_bytes = 2000000
    try:
        buf = open_file(app, p)
        app.logs.search = 'a*b'
        before = time.monotonic()
        assert tools.count_retained(app, buf) == 1
        assert tools.find_retained(app, buf) == (True, 1)
        assert not tools.retained_matches(app, buf.lines[0])
        assert app.log_tools_state['retained_omitted'] == 1
        assert time.monotonic() - before < 1
        app.logs.search = '(a+)+$'
        assert tools.count_retained(app, buf) == 0
        assert not tools.retained_matches(app, 'a' * 1000 + 'x')
        app.logs.search = '['
        assert tools.retained_matches(app, 'literal [ here')
    finally: app.research.close()


def test_page_horizontal_pan_is_bounded_cached_and_keeps_raw_bytes(tmp_path, monkeypatch):
    from raven import log_scan
    p = tmp_path / 'job.out'
    p.write_text('界' * 60000 + 'end\n')
    app = app_for(tmp_path)
    try:
        open_file(app, p)
        tools.run_command(app, ['logolder', '0'])
        complete(app)
        page = app.log_tools_state['page']
        original = page['rows'][0]['raw']
        app.log_tools_state['page_pan'] = 90000
        count = []
        actual = tools._pan
        def tracked(*args, **kwargs):
            count.append(True)
            result = actual(*args, **kwargs)
            assert len(result) <= 800
            return result
        monkeypatch.setattr(tools, '_pan', tracked)
        views = SimpleNamespace(g=L.Glyphs(False))
        for _ in range(25): tools.overlay(views, {}, app, 100, 26)
        assert len(count) == 1
        assert page['rows'][0]['raw'] == original
    finally: app.research.close()


@pytest.mark.parametrize('command', [['logolder', '0'], ['logsearch', 'error'], ['loggoto', 'line', '1']])
def test_background_log_completion_does_not_reopen_after_navigation(tmp_path, command):
    p = tmp_path / 'job.out'
    p.write_text('error\n')
    app = app_for(tmp_path)
    try:
        open_file(app, p)
        tools.run_command(app, command)
        app.tab = 'jobs'
        app.mode = 'main'
        complete(app)
        assert app.tab == 'jobs' and app.mode == 'main'
        assert app.log_tools_state['page'] is not None or app.log_tools_state['results'] is not None
    finally: app.research.close()


def test_closed_source_page_stays_closed_when_next_page_finishes(tmp_path):
    p = tmp_path / 'job.out'
    p.write_text('line\n' * 2000)
    app = app_for(tmp_path)
    try:
        open_file(app, p)
        tools.run_command(app, ['logolder', '0'])
        complete(app)
        tools.handle_key(app, ']')
        tools.handle_key(app, 'esc')
        complete(app)
        assert app.mode == 'main'
    finally: app.research.close()


def test_mark_from_long_line_fragment_reopens_same_fragment(tmp_path):
    from raven import log_scan
    p = tmp_path / 'job.out'
    p.write_bytes(b'x' * log_scan.MAX_LINE_BYTES + b'FAILED fragment\nlast\n')
    app = app_for(tmp_path)
    try:
        open_file(app, p)
        tools.run_command(app, ['logsearch', 'FAILED'])
        complete(app)
        tools.handle_key(app, 'enter')
        complete(app)
        assert app.log_tools_state['page']['rows'][0]['line'] == 1
        tools.run_command(app, ['logmark', 'fragment failure'])
        tools.run_command(app, ['logmarks'])
        tools.handle_key(app, 'enter')
        complete(app)
        assert app.log_tools_state['page']['rows'][0]['line'] == 1
        assert 'FAILED fragment' in app.log_tools_state['page']['rows'][0]['text']
    finally: app.research.close()


def test_finished_hidden_tail_read_is_published_before_source_page_admission(tmp_path):
    from concurrent.futures import Future
    p = tmp_path / 'job.out'
    p.write_text('error\ncontext\n')
    app = app_for(tmp_path)
    published = []
    try:
        open_file(app, p)
        tools.run_command(app, ['logsearch', 'error'])
        complete(app)
        future = Future()
        future.set_result('cached tail')
        app.research.future = future
        app.research.pending = (future, published.append)
        assert tools.handle_key(app, 'enter')
        assert app.log_tools_state['busy']
        assert published == ['cached tail']
        complete(app)
        assert app.mode == 'log_tools_page'
        assert app.log_tools_state['page']['path'] == str(p)
        assert app.log_tools_state['page']['rows'][0]['text'] == 'error'
    finally: app.research.close()


def test_unfinished_reader_is_rejected_without_waiting_or_queueing(tmp_path):
    from concurrent.futures import Future
    import time
    p = tmp_path / 'job.out'
    p.write_text('error\n')
    app = app_for(tmp_path)
    try:
        open_file(app, p)
        future = Future()
        app.research.future = future
        pending = (future, lambda result: None)
        app.research.pending = pending
        started = time.monotonic()
        assert tools.run_command(app, ['logolder', '0'])
        assert time.monotonic() - started < .2
        assert app.research.pending is pending
        assert not app.log_tools_state['busy'] and app.mode == 'main'
        assert 'background reader is busy' in app.message.lower()
    finally:
        app.research.pending = None
        app.research.close()
