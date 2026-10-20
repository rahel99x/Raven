"""Interactive reads stay responsive when CARC's shared filesystem stalls."""
from __future__ import annotations

from threading import Event, current_thread
import time

import pytest

from raven.logs import LogSession
from raven.remote import LocalFiles
from raven.research import ResearchHub


class SharedFiles(LocalFiles):
    def __init__(self):
        self.calls = []
        self.entered, self.release = Event(), Event()
        self.block = ""

    def _call(self, operation):
        thread = current_thread().name
        assert thread.startswith("raven-research"), "Shared filesystem I/O reached the terminal thread"
        self.calls.append((operation, thread))
        if self.block == operation:
            self.entered.set()
            assert self.release.wait(5), "Filesystem worker was not released"

    def snapshot_stat(self, path):
        self._call("stat")
        return super().snapshot_stat(path)

    def read(self, path, offset, length):
        self._call("read")
        return super().read(path, offset, length)


def finish(worker):
    worker.future.result(timeout=5)
    worker.poll_task()


def load(session, worker, path):
    session.buffer(str(path), worker=worker, background=True)
    finish(worker)
    return session.buffer(str(path), worker=worker, background=True)


@pytest.mark.parametrize("operation", ["stat", "read"])
def test_blocked_local_load_never_blocks_key_navigation(operation, tmp_path):
    path = tmp_path / "job.log"
    original = b"AB first\tcolumn\r\nCD second\n"
    path.write_bytes(original)
    files = SharedFiles()
    files.block = operation
    worker, session = ResearchHub({}, files), LogSession(files=files)
    try:
        cold = session.buffer(str(path), worker=worker, background=True)
        assert cold.loading and cold.total == 0
        assert files.entered.wait(1)
        future = worker.future
        began = time.perf_counter()
        for _ in range(200):
            current = session.buffer(str(path), worker=worker, background=True)
            assert current is cold and current.loading
            session.move_cursor("home", current)
            session.move_cursor("up", current)
            assert not session.begin_selection(current)
        assert time.perf_counter() - began < .2
        assert worker.future is future
        files.release.set()
        finish(worker)
        ready = session.buffer(str(path), worker=worker, background=True)
        assert not ready.loading and ready is not cold
        assert ready.raw_range(0, 1) == original
        assert cold.total == 0
    finally:
        files.release.set()
        worker.close()


@pytest.mark.parametrize("operation", ["stat", "read"])
def test_blocked_local_refresh_keeps_existing_snapshot_scrollable(operation, tmp_path):
    path = tmp_path / "job.log"
    original = b"AB first\tcolumn\r\nCD second\n"
    path.write_bytes(original)
    files = SharedFiles()
    worker, session = ResearchHub({}, files), LogSession(files=files)
    try:
        ready = load(session, worker, path)
        assert session.begin_selection(ready, 0)
        session.move_cursor("down", ready)
        token = session._buffer_token
        files.block = operation
        if operation == "read":
            with path.open("ab") as stream:
                stream.write(b"new output\n")
        session.invalidate_remote()
        session.buffer(str(path), worker=worker, background=True)
        assert files.entered.wait(1)
        future = worker.future
        calls = len(files.calls)
        for _ in range(200):
            current = session.buffer(str(path), worker=worker, background=True)
            assert current is ready and not current.loading
            session.move_cursor("home", current)
            session.move_cursor("up", current)
        assert session.cursor == 0 and session._buffer_token == token
        assert worker.future is future and len(files.calls) == calls
        assert ready.raw_range(0, 1) == original
    finally:
        files.release.set()
        worker.close()


@pytest.mark.parametrize("change", ["append", "rotate", "truncate", "evict"])
def test_local_snapshot_changes_preserve_only_valid_selection(change, tmp_path):
    path = tmp_path / "job.log"
    original = b"zero\none\ntwo\n"
    path.write_bytes(original)
    files = SharedFiles()
    worker, session = ResearchHub({}, files), LogSession(files=files, max_bytes=32)
    try:
        ready = load(session, worker, path)
        session.begin_selection(ready, 0)
        session.move_cursor("down", ready)
        selected = session.selection_bytes(ready)
        if change == "append":
            with path.open("ab") as stream:
                stream.write(b"three\n")
        elif change == "rotate":
            path.rename(tmp_path / "old.log")
            path.write_bytes(b"replacement\n")
        elif change == "truncate":
            path.write_bytes(b"x\n")
        else:
            with path.open("ab") as stream:
                stream.write(b"new output\n" * 20)
        session.invalidate_remote()
        latest = load(session, worker, path)
        assert latest is not ready and ready.raw_range(0, 2) == original
        if change == "append":
            assert session.selection_active and session.selection_bytes(latest) == selected
            assert latest.total == 4
        else:
            assert not session.selection_active and session.cursor is None
            if change == "evict":
                assert latest.truncated and latest.skipped_bytes > 0
                assert latest.raw_range(0, latest.total - 1).endswith(b"new output\n")
            else:
                assert latest.raw_range(0, 0) == path.read_bytes()
    finally:
        worker.close()


def test_local_polling_coalesces_until_due_and_refresh_retries(tmp_path, monkeypatch):
    path = tmp_path / "job.log"
    path.write_bytes(b"first\nsecond\n")
    files = SharedFiles()
    worker, session = ResearchHub({}, files), LogSession(files=files)
    now = [100.0]
    monkeypatch.setattr("raven.logs.time.monotonic", lambda: now[0])
    try:
        ready = load(session, worker, path)
        calls = len(files.calls)
        for _ in range(100):
            assert session.buffer(str(path), worker=worker, background=True) is ready
        assert len(files.calls) == calls
        now[0] += .6
        session.buffer(str(path), worker=worker, background=True)
        finish(worker)
        assert len(files.calls) == calls + 1
        session.invalidate_remote()
        session.buffer(str(path), worker=worker, background=True)
        finish(worker)
        assert len(files.calls) == calls + 2
    finally:
        worker.close()


def test_busy_worker_defers_local_log_without_synchronous_fallback(tmp_path):
    path = tmp_path / "job.log"
    path.write_bytes(b"first\nsecond\n")
    files = SharedFiles()
    worker, session = ResearchHub({}, files), LogSession(files=files)
    release = Event()
    try:
        assert worker.start_task(lambda: release.wait(5), lambda result: None)
        future = worker.future
        for _ in range(100):
            cold = session.buffer(str(path), worker=worker, background=True)
            assert cold.loading and cold.total == 0
        assert files.calls == [] and worker.future is future
        assert session._async_pending is None
        release.set()
        finish(worker)
        assert load(session, worker, path).total == 2
    finally:
        release.set()
        worker.close()


def test_deleted_local_source_retains_snapshot_with_visible_error(tmp_path):
    path = tmp_path / "job.log"
    original = b"first\nsecond\n"
    path.write_bytes(original)
    files = SharedFiles()
    worker, session = ResearchHub({}, files), LogSession(files=files)
    try:
        ready = load(session, worker, path)
        session.begin_selection(ready, 0)
        path.unlink()
        session.invalidate_remote()
        failed = load(session, worker, path)
        assert failed.error and not failed.loading
        assert failed.raw_lines == ready.raw_lines and ready.error == ""
        assert not session.selection_active
    finally:
        worker.close()


def test_pending_local_read_cannot_publish_over_different_file(tmp_path):
    old_path, new_path = tmp_path / "old.log", tmp_path / "new.log"
    old_path.write_bytes(b"old job output\n")
    new_path.write_bytes(b"new job output\n")
    files = SharedFiles()
    files.block = "read"
    worker, session = ResearchHub({}, files), LogSession(files=files)
    try:
        old = session.buffer(str(old_path), worker=worker, background=True)
        assert files.entered.wait(1)
        new = session.buffer(str(new_path), worker=worker, background=True)
        assert new.loading and session.path == str(new_path)
        files.release.set()
        finish(worker)
        assert session.buffers[str(old_path)] is old and old.total == 0
        assert session.buffers[str(new_path)] is new and new.total == 0
        ready = load(session, worker, new_path)
        assert ready.raw_range(0, 0) == b"new job output\n"
        assert session.path == str(new_path)
    finally:
        files.release.set()
        worker.close()


def test_unchanged_local_poll_does_not_copy_retained_containers(tmp_path, monkeypatch):
    from raven.logs import LogBuffer
    path = tmp_path / "job.log"
    path.write_bytes(b"first\nsecond\n" * 5000)
    files = SharedFiles()
    worker, session = ResearchHub({}, files), LogSession(files=files)
    try:
        ready = load(session, worker, path)
        def forbidden(_buffer):
            raise AssertionError("Unchanged poll copied the retained log")
        monkeypatch.setattr(LogBuffer, "_worker_copy", forbidden)
        session.invalidate_remote()
        current = load(session, worker, path)
        assert not current.error and current.lines is ready.lines
        assert current.raw_lines is ready.raw_lines
        assert current.raw_range(0, 1) == b"first\nsecond\n"
    finally:
        worker.close()


def test_real_controller_top_navigation_uses_warmed_snapshot_during_filesystem_stall(tmp_path):
    from raven.config import Config
    from raven.controller import App
    from raven.layout import Glyphs, row_text
    from raven.model import Finished, Store
    from raven.views import Views
    path = tmp_path / "job.log"
    path.write_bytes(b"".join(f"AB original line {number}\n".encode() for number in range(2000)))
    files = SharedFiles()
    cfg = Config({"log_lines": 0})
    store = Store(persist=False)
    store.finished = [Finished("77", "experiment", "FAILED", workdir=str(tmp_path))]
    store.details["77"] = {"StdOut": str(path), "WorkDir": str(tmp_path)}
    app = App(store, None, None, cfg, "test", ascii_=False)
    app.files = app.logs.files = files
    app.research = ResearchHub(cfg, files)
    views = Views(Glyphs(False), cfg, files=files)
    app.views_ref = views
    try:
        app.open_log("77")
        views.compose(store.snapshot(), app, 100, 24)
        finish(app.research)
        views.compose(store.snapshot(), app, 100, 24)
        ready = app.logs.buffers[str(path)]
        files.block = "stat"
        app.logs.invalidate_remote()
        views.compose(store.snapshot(), app, 100, 24)
        assert files.entered.wait(1)
        future = app.research.future
        calls = len(files.calls)
        for key in ["home", "up", "pgup", "down", "end", "home"] * 5:
            app.handle(key)
            app.tick()
            rows, _ = views.compose(store.snapshot(), app, 100, 24)
        assert app.logs.cursor == 0
        assert "AB original line 0" in "\n".join(row_text(row) for row in rows)
        assert app.logs.buffers[str(path)] is ready
        assert app.research.future is future and len(files.calls) == calls
        assert all(thread.startswith("raven-research") for _, thread in files.calls)
    finally:
        files.release.set()
        app.research.close()


def test_real_interactive_local_snapshot_yanks_original_bytes_after_pan(tmp_path, monkeypatch):
    from raven import clipboard
    from raven.config import Config
    from raven.controller import App
    from raven.layout import Glyphs, row_text
    from raven.model import Finished, Store
    from raven.views import Views
    path = tmp_path / "job.log"
    original = "AB first\tcolumn 界\r\nCD second\r\nEF third\n".encode()
    path.write_bytes(original)
    files = SharedFiles()
    cfg = Config({"log_lines": 0, "clipboard": {"osc52": False, "tools": False}})
    store = Store(persist=False)
    store.finished = [Finished("77", "experiment", "FAILED", workdir=str(tmp_path))]
    store.details["77"] = {"StdOut": str(path), "WorkDir": str(tmp_path)}
    app = App(store, None, None, cfg, "test", ascii_=False)
    app.files = app.logs.files = files
    app.research = ResearchHub(cfg, files)
    views = Views(Glyphs(False), cfg, files=files)
    app.views_ref = views
    copied = []
    monkeypatch.setattr(clipboard, "copy", lambda text, state_dir=None, **kw: copied.append(text.encode()) or "copied snapshot")
    try:
        app.open_log("77")
        views.compose(store.snapshot(), app, 100, 24)
        finish(app.research)
        views.compose(store.snapshot(), app, 100, 24)
        app.handle("home")
        app.handle("v")
        app.handle("down")
        app.run_command("logpan 2")
        rows, _ = views.compose(store.snapshot(), app, 100, 24)
        assert "pan 2" in "\n".join(row_text(row) for row in rows)
        app.handle("y")
        for future in app.clipboard_pending:
            future.result(timeout=5)
        clipboard.poll(app)
        assert copied == ["AB first\tcolumn 界\r\nCD second\r\n".encode()]
        app.run_command("logpan 0")
        rows, _ = views.compose(store.snapshot(), app, 100, 24)
        assert "AB first" in "\n".join(row_text(row) for row in rows)
        assert app.logs.buffers[str(path)].raw_range(0, 2) == original
    finally:
        app.research.close()


def test_same_size_local_rewrite_reloads_without_mutating_published_snapshot(tmp_path):
    from types import SimpleNamespace
    from raven import log_tools
    path = tmp_path / 'job.log'
    original, replacement = b'ERROR first\nsecond\n', b'plain first\nsecond\n'
    assert len(original) == len(replacement)
    path.write_bytes(original)
    files = SharedFiles()
    worker, session = ResearchHub({}, files), LogSession(files=files)
    app = SimpleNamespace(logs=session, research=worker, interactive=True, cfg={}, log_job='77', project_state={}, mode='main', tab='log')
    app.say = app.fail = lambda message: None
    log_tools.initialize(app)
    try:
        ready = load(session, worker, path)
        session.search = 'ERROR'
        log_tools.count_retained(app, ready)
        if app.log_tools_state['retained_pending']:
            finish(worker)
        assert log_tools.count_retained(app, ready) == 1
        assert session.begin_selection(ready, 0)
        token = session._buffer_token
        log_tools.observe_buffer(app, ready)
        updated = ready.source_updated
        path.write_bytes(replacement)
        session.invalidate_remote()
        latest = load(session, worker, path)
        assert latest.ident == ready.ident and latest.size == ready.size
        assert latest.source_updated != updated and latest.reloads == ready.reloads + 1
        assert latest.raw_range(0, 1) == replacement
        assert ready.raw_range(0, 1) == original and ready.source_updated == updated
        assert session._buffer_token != token and not session.selection_active
        assert session.cursor is None and session.top is None
        log_tools.observe_buffer(app, latest)
        assert all(saved['identity'][1] == latest.reloads for saved in app.log_tools_state['positions'].values())
        log_tools.count_retained(app, latest)
        if app.log_tools_state['retained_pending']:
            finish(worker)
        assert log_tools.count_retained(app, latest) == 0
    finally: worker.close()


def test_rewrite_during_worker_reload_rejects_half_refreshed_local_data(tmp_path):
    path = tmp_path / 'job.log'
    original, replacement, raced = b'first\n', b'other\n', b'final\n'
    path.write_bytes(original)
    class Rewriting(SharedFiles):
        rewrite = False
        def read(self, path_, offset, length):
            raw = super().read(path_, offset, length)
            if self.rewrite:
                path.write_bytes(raced)
            return raw
    files = Rewriting()
    worker, session = ResearchHub({}, files), LogSession(files=files)
    try:
        ready = load(session, worker, path)
        session.begin_selection(ready, 0)
        path.write_bytes(replacement)
        files.rewrite = True
        session.invalidate_remote()
        rejected = load(session, worker, path)
        assert rejected.error and 'changed during inspection' in rejected.error
        assert rejected.raw_lines == ready.raw_lines and not session.selection_active
        assert rejected.source_updated == ready.source_updated
        assert ready.raw_range(0, 0) == original
        files.rewrite = False
        session.invalidate_remote()
        latest = load(session, worker, path)
        assert not latest.error and latest.raw_range(0, 0) == raced
        assert latest.reloads == ready.reloads + 1
    finally: worker.close()
