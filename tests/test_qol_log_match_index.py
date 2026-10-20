from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pytest

from raven import log_match_index as index, log_tools
from raven.logs import LogBuffer, LogSession
from raven.remote import LocalFiles
from raven.research import ResearchHub


def buffer(lines, partial=b'', skipped=0):
    buf = LogBuffer('/fixture/job.out', max_bytes=64 << 20)
    buf.ident = (1, 22)
    buf.lines = list(lines)
    buf.raw_lines = [line.encode() for line in lines]
    buf._line_bytes = [len(line) + 1 for line in buf.raw_lines]
    buf._partial_raw, buf.partial = partial, partial.decode('utf-8', 'replace')
    buf.skipped_bytes = skipped
    buf._retained_bytes = sum(buf._line_bytes) + len(partial)
    buf.size = skipped + buf._retained_bytes
    return buf


def appended(old, lines=(), partial=None, drop=0):
    buf = old._worker_copy()
    extra = list(lines)
    buf.lines = old.lines[drop:] + extra
    buf.raw_lines = old.raw_lines[drop:] + [line.encode() for line in extra]
    buf._line_bytes = old._line_bytes[drop:] + [len(raw) + 1 for raw in buf.raw_lines[len(old.lines) - drop:]]
    buf.skipped_bytes = old.skipped_bytes + sum(old._line_bytes[:drop])
    buf._partial_raw = old._partial_raw if partial is None else partial
    buf.partial = buf._partial_raw.decode('utf-8', 'replace')
    buf._retained_bytes = sum(buf._line_bytes) + len(buf._partial_raw)
    buf.size = buf.skipped_bytes + buf._retained_bytes
    return buf


def app_for():
    app = SimpleNamespace(logs=LogSession(), interactive=True, cfg={},
        research=ResearchHub({}, LocalFiles()), mode='main', tab='log', message='')
    app.logs.search = 'ERROR'
    app.say = lambda value: setattr(app, 'message', value)
    app.fail = lambda value: setattr(app, 'message', value)
    log_tools.initialize(app)
    return app


def complete(app):
    app.research.future.result(timeout=5)
    app.research.poll_task()


def test_append_and_prefix_trim_only_scan_new_completed_rows():
    class Matcher:
        def __init__(self): self.calls = []
        def search(self, text, *args):
            self.calls.append(text)
            return re.search('ERROR', text)
    matcher = Matcher()
    old = buffer(['ERROR', 'plain', 'ERROR', 'plain'])
    initial = index.build(old, ('same',), matcher, False)
    assert initial.total_count == 2
    matcher.calls.clear()
    new = appended(old, ['ERROR', 'new plain'], drop=2)
    result = index.extend(initial, new, ('same',), matcher, regex=False, query_length=5)
    assert result.total_count == 2 and matcher.calls == ['ERROR', 'new plain']
    assert result.flags == b'\x01\x00\x01\x00'
    assert index.find(result, None) == 0
    assert index.find(result, 0) == 2
    assert index.find(result, 2) == 0
    assert index.find(result, None, True) == 2


def test_partial_literal_suffix_and_word_end_are_adjusted_correctly():
    matcher = re.compile(r'\bERROR\b')
    old = buffer(['plain'], b'ERROR')
    initial = index.build(old, ('same',), matcher, False)
    assert initial.total_count == 1
    new = appended(old, partial=b'ERRORs')
    changed = index.extend(initial, new, ('same',), matcher, regex=False, query_length=5, word=True)
    assert changed.total_count == 0
    final = appended(new, ['ERRORs', 'ERROR'], partial=b'')
    result = index.extend(changed, final, ('same',), matcher, regex=False, query_length=5, word=True)
    assert result.total_count == 1 and result.partial_flag == 0


def test_invalid_prefix_rotation_and_in_place_rewrite_request_rebuild():
    old = buffer(['same', 'ERROR', 'same'])
    matcher = re.compile('ERROR')
    initial = index.build(old, ('same',), matcher, False)
    replaced = old._worker_copy()
    replaced.lines[1] = 'plain'
    assert index.extend(initial, replaced, ('same',), matcher, regex=False, query_length=5) is None
    jumped = appended(old, ['new'])
    jumped.skipped_bytes += 1
    assert index.extend(initial, jumped, ('same',), matcher, regex=False, query_length=5) is None
    assert index.extend(initial, old, ('changed-query',), matcher, regex=False, query_length=5) is None


def test_large_initial_interactive_index_is_actual_background_task():
    app = app_for()
    buf = buffer(['plain'] * 10000 + ['ERROR'])
    try:
        assert log_tools.count_retained(app, buf) == 0
        state = app.log_tools_state
        assert state['retained_pending'] and state['retained_known_count'] is None
        assert app.research.pending is not None
        complete(app)
        assert log_tools.count_retained(app, buf) == 1
        assert not state['retained_pending'] and state['retained_processed'] == 10001
        assert state['retained_total'] == 10001
    finally: app.research.close()


def test_max_retention_append_trim_count_and_missing_find_have_no_line_rescan():
    app = app_for()
    app.logs.search = 'MISSING'
    line = 'x' * 150
    old = buffer([line] * 200000)
    try:
        log_tools.count_retained(app, old)
        complete(app)
        assert log_tools.count_retained(app, old) == 0
        for _ in range(5):
            new = appended(old, [line, line], drop=2)
            started = time.monotonic()
            assert log_tools.count_retained(app, new) == 0
            assert log_tools.find_retained(app, new) == (True, None)
            assert time.monotonic() - started < .05
            assert not app.log_tools_state['retained_pending']
            assert len(app.log_tools_state['retained_index'].flags) == 200000
            old = new
    finally: app.research.close()


def test_large_equal_size_rewrite_and_query_change_eventually_replace_index():
    app = app_for()
    old = buffer(['same'] * 10000)
    try:
        log_tools.count_retained(app, old)
        complete(app)
        rewritten = old._worker_copy()
        rewritten.lines[5000] = 'ERROR'
        assert log_tools.count_retained(app, rewritten) == 0
        assert app.log_tools_state['retained_pending']
        complete(app)
        assert log_tools.count_retained(app, rewritten) == 1
        app.logs.search = 'same'
        assert log_tools.count_retained(app, rewritten) == 0
        assert app.log_tools_state['retained_known_count'] is None
        complete(app)
        assert log_tools.count_retained(app, rewritten) == 9999
    finally: app.research.close()


def test_new_query_cannot_receive_prior_pending_worker_results():
    app = app_for()
    buf = buffer(['ERROR'] * 10000)
    try:
        log_tools.count_retained(app, buf)
        app.logs.search = 'MISSING'
        complete(app)
        assert app.log_tools_state['retained_index'] is None
        log_tools.count_retained(app, buf)
        complete(app)
        assert log_tools.count_retained(app, buf) == 0
    finally: app.research.close()


def test_explicit_headless_reads_refresh_same_mutable_buffer(tmp_path):
    from raven.logs import LogSession
    path = tmp_path / 'legacy.log'
    path.write_bytes(b'ERROR\n')
    app = SimpleNamespace(logs=LogSession(), interactive=False, research=None, cfg={}, mode='main', tab='log')
    app.say = app.fail = lambda value: None
    log_tools.initialize(app)
    try:
        buf = app.logs.buffer(str(path))
        app.logs.search = 'ERROR'
        assert log_tools.count_retained(app, buf) == 1
        initial = app.log_tools_state['retained_index']
        with path.open('ab') as stream: stream.write(b'ERROR\n')
        assert app.logs.buffer(str(path)) is buf
        assert log_tools.count_retained(app, buf) == 2
        assert initial.stamp != app.log_tools_state['retained_index'].stamp
        path.write_bytes(b'plain\n')
        assert app.logs.buffer(str(path)) is buf
        assert log_tools.count_retained(app, buf) == 0
        assert app.research is None
    finally:
        if app.research is not None: app.research.close()


def test_same_mutable_buffer_partial_word_match_is_not_reused_after_append(tmp_path):
    from raven.logs import LogSession
    path = tmp_path / 'partial.log'
    path.write_bytes(b'ERROR')
    app = SimpleNamespace(logs=LogSession(), interactive=False, research=None, cfg={}, mode='main', tab='log')
    app.say = app.fail = lambda value: None
    state = log_tools.initialize(app)
    state['search_mode'] = {'regex': False, 'case': True, 'word': True}
    state['retained_explicit'] = True
    buf = app.logs.buffer(str(path))
    app.logs.search = 'ERROR'
    assert log_tools.count_retained(app, buf) == 1
    with path.open('ab') as stream: stream.write(b's')
    assert app.logs.buffer(str(path)) is buf
    assert log_tools.count_retained(app, buf) == 0
    assert log_tools.find_retained(app, buf) == (True, None)
    assert app.research is None


def test_pending_search_navigates_after_its_matching_index_is_published():
    app = app_for()
    buf = buffer(["plain"] * 40 + ["ERROR"] + ["plain"] * 40)
    app.logs.path = buf.path
    app.logs.sync_buffer(buf)
    app.message = "Indexing retained log in the background; navigation remains available"
    try:
        assert log_tools.find_retained(app, buf, backwards=True) == (True, None)
        assert app.logs.match is None and app.logs.following
        complete(app)
        assert app.logs.match == app.logs.cursor == 40
        assert not app.logs.following and app.message.startswith("1 lines match")
        assert app.log_tools_state["retained_navigation"] is None
    finally:
        app.research.close()


@pytest.mark.parametrize("change", ["cursor", "query", "source", "tab"])
def test_pending_search_cannot_override_a_later_user_choice(change):
    app = app_for()
    buf = buffer(["ERROR"] + ["plain"] * 80)
    app.logs.path = buf.path
    app.logs.sync_buffer(buf)
    try:
        assert log_tools.find_retained(app, buf, backwards=True) == (True, None)
        if change == "cursor":
            app.logs.goto(70, buf)
        elif change == "query":
            app.logs.search = "different"
        elif change == "source":
            app.logs.path = "/another-source.out"
        else:
            app.tab = "jobs"
        complete(app)
        assert app.logs.match is None
        assert app.logs.cursor == (70 if change == "cursor" else None)
        assert app.log_tools_state["retained_navigation"] is None
    finally:
        app.research.close()
