"""Remote log frames and key navigation use immutable worker snapshots."""
from __future__ import annotations

from threading import Event, current_thread
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from raven.logs import LogSession
from raven.remote import LocalFiles
from raven.research import ResearchHub


class Remote:
    remote = True
    min_refresh = 0
    def __init__(self, data=b"first\tcolumn\r\nsecond\n"):
        self.data, self.ident, self.updated = data, (1, 77), 1
        self.calls = []
        self.entered, self.release = Event(), Event()
        self.block = False
        self.short = False
        self.error = ""
        self.after_read = None
    def _call(self, name):
        thread = current_thread().name
        assert thread.startswith("raven-research"), "SSH cannot run on the terminal thread"
        self.calls.append((name, thread))
        if self.error:
            raise OSError(self.error)
    def snapshot_stat(self, path):
        self._call("stat:" + path)
        return {"size": len(self.data), "ident": self.ident, "updated": (self.updated, self.updated)}
    def read(self, path, offset, count):
        self._call("read:" + path)
        self.entered.set()
        if self.block:
            assert self.release.wait(3)
        data = self.data[offset:offset + count]
        if self.short:
            data = data[:-1]
        if self.after_read:
            self.after_read()
        return data


def finish(hub):
    hub.future.result(timeout=5)
    hub.poll_task()


def load(session, hub, path="remote.log"):
    session.buffer(path, worker=hub, background=True)
    finish(hub)
    return session.buffer(path, worker=hub, background=True)


def test_blocking_ssh_does_not_block_cold_render_or_keyboard_buffer_reads():
    files = Remote()
    files.block = True
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        cold = session.buffer("remote.log", worker=hub, background=True)
        assert cold.loading and cold.total == 0 and not cold.error
        assert files.entered.wait(1)
        future = hub.future
        start = time.perf_counter()
        for _ in range(200):
            buf = session.buffer("remote.log", worker=hub, background=True)
            assert buf is cold and buf.loading
            session.move_cursor("down", buf)
            assert not session.begin_selection(buf)
        assert time.perf_counter() - start < .2
        assert hub.future is future and len(files.calls) == 2
        files.release.set()
        finish(hub)
        ready = session.buffer("remote.log", worker=hub, background=True)
        assert ready is not cold and not ready.loading
        assert ready.raw_range(0, 1) == files.data
        assert cold.total == 0  # The worker did not mutate an already drawn snapshot.
    finally:
        files.release.set()
        hub.close()


def test_append_publishes_new_snapshot_but_preserves_raw_selection():
    files = Remote()
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        original = load(session, hub)
        assert session.begin_selection(original, 0)
        session.move_cursor("down", original)
        token = session._buffer_token
        expected = session.selection_bytes(original)
        files.data += b"third\r\n"
        files.updated += 1
        session.invalidate_remote()
        session.buffer("remote.log", worker=hub, background=True)
        finish(hub)
        latest = session.buffer("remote.log", worker=hub, background=True)
        assert latest is not original and original.total == 2 and latest.total == 3
        assert session._buffer_token == token
        assert session.selection_active and session.selection_bytes(latest) == expected
        assert latest.raw_range(0, 2) == files.data
    finally:
        hub.close()


@pytest.mark.parametrize("change", ["rotate", "truncate", "evict"])
def test_source_identity_changes_invalidate_cached_selection(change):
    files = Remote(b"zero\none\ntwo\n")
    hub, session = ResearchHub({}, files), LogSession(max_bytes=32, files=files)
    try:
        original = load(session, hub)
        session.begin_selection(original, 0)
        if change == "rotate":
            files.ident, files.data = (1, 78), b"replacement\n"
        elif change == "truncate":
            files.data = b"x\n"
        else:
            files.data += b"new line\n" * 20
        files.updated += 1
        session.invalidate_remote()
        session.buffer("remote.log", worker=hub, background=True)
        finish(hub)
        latest = session.buffer("remote.log", worker=hub, background=True)
        assert not session.selection_active and session.cursor is None
        assert session.selection_bytes(latest) == b""
    finally:
        hub.close()


def test_late_result_from_previous_path_cannot_replace_current_snapshot():
    files = Remote()
    files.block = True
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        old = session.buffer("old.log", worker=hub, background=True)
        assert files.entered.wait(1)
        new = session.buffer("new.log", worker=hub, background=True)
        assert session.path == "new.log" and new.loading
        files.release.set()
        finish(hub)
        assert session.buffers["old.log"] is old and old.total == 0
        assert session.buffers["new.log"] is new and new.total == 0
        session.buffer("new.log", worker=hub, background=True)
        finish(hub)
        ready = session.buffer("new.log", worker=hub, background=True)
        assert ready.total == 2 and session.path == "new.log"
    finally:
        files.release.set()
        hub.close()


def test_switching_backend_rejects_late_remote_data_without_local_fallback(tmp_path):
    path = tmp_path / "job.log"
    path.write_bytes(b"local private data\n")
    remote = Remote(b"remote data\n")
    remote.block = True
    hub, session = ResearchHub({}, remote), LogSession(files=remote)
    try:
        session.buffer(str(path), worker=hub, background=True)
        assert remote.entered.wait(1)
        session.files = LocalFiles()
        local = session.buffer(str(path), worker=hub, background=True)
        assert local.loading and local.total == 0
        remote.release.set()
        finish(hub)
        assert session.buffers[str(path)] is local
        assert local.loading and local.total == 0
        session.buffer(str(path), worker=hub, background=True)
        finish(hub)
        ready = session.buffer(str(path), worker=hub, background=True)
        assert ready is not local and not ready.loading
        assert ready.raw_range(0, 0) == b"local private data\n"
        assert b"remote data" not in ready.raw_range(0, 0)
    finally:
        remote.release.set()
        hub.close()


def test_remote_adapter_failure_retains_previous_complete_bytes_with_visible_error():
    files = Remote()
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        original = load(session, hub)
        session.begin_selection(original, 0)
        files.error = "SSH unavailable"
        session.invalidate_remote()
        session.buffer("remote.log", worker=hub, background=True)
        finish(hub)
        latest = session.buffer("remote.log", worker=hub, background=True)
        assert latest.error == "SSH unavailable" and not latest.loading
        assert latest.raw_lines == original.raw_lines and original.error == ""
        assert not session.selection_active
    finally:
        hub.close()


def test_short_read_is_not_published_as_complete_remote_log():
    files = Remote()
    files.short = True
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        latest = load(session, hub)
        assert latest.total == 0 and "incomplete" in latest.error
        assert not latest.loading
    finally:
        hub.close()


@pytest.mark.parametrize("mutation", ["rotate", "shrink", "same_size"])
def test_file_change_during_ssh_read_rejects_half_refreshed_data(mutation):
    files = Remote()
    def mutate():
        if mutation == "rotate":
            files.ident = (1, 78)
        elif mutation == "shrink":
            files.data = b"x\n"
        files.updated += 1
    files.after_read = mutate
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        latest = load(session, hub)
        assert latest.total == 0
        assert "changed during inspection" in latest.error
    finally:
        hub.close()


def test_append_during_ssh_read_publishes_initial_bounded_snapshot():
    files = Remote()
    initial = files.data
    def append():
        files.data += b"third\n"
        files.updated += 1
    files.after_read = append
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        latest = load(session, hub)
        assert not latest.error and latest.raw_range(0, 1) == initial
        assert latest.size == len(initial)
    finally:
        hub.close()


def test_busy_worker_and_missing_worker_never_invoke_ssh_on_terminal_thread():
    files = Remote()
    session = LogSession(files=files)
    hub = ResearchHub({}, files)
    release = Event()
    try:
        cold = session.buffer("remote.log", background=True)
        assert cold.loading and files.calls == []
        hub.start_task(lambda: release.wait(2), lambda result: None)
        future = hub.future
        for _ in range(50):
            assert session.buffer("remote.log", worker=hub, background=True) is cold
        assert files.calls == [] and hub.future is future
        assert session._async_pending is None
        release.set()
        finish(hub)
        assert load(session, hub).total == 2
    finally:
        release.set()
        hub.close()


def test_remote_polling_deadline_is_coalesced_and_explicit_refresh_retries():
    files = Remote()
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        ready = load(session, hub)
        calls = len(files.calls)
        for _ in range(100):
            assert session.buffer("remote.log", worker=hub, background=True) is ready
        assert len(files.calls) == calls
        session.invalidate_remote()
        session.buffer("remote.log", worker=hub, background=True)
        finish(hub)
        assert len(files.calls) == calls + 1  # Unchanged size needs metadata only.
    finally:
        hub.close()


def test_local_default_reader_remains_synchronous_and_lossless(tmp_path):
    path = tmp_path / "job.log"
    path.write_bytes(b"first\tcolumn\r\nsecond\n")
    session = LogSession()
    buf = session.buffer(str(path), background=True)
    assert not buf.loading and buf.raw_range(0, 1) == path.read_bytes()
    assert session._async_pending is None


def test_cache_replacement_cannot_reuse_selection_snapshot_token():
    files = Remote()
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        original = load(session, hub)
        session.begin_selection(original, 0)
        token = session._buffer_token
        session.buffers.clear()
        session.invalidate_remote()
        replacement = load(session, hub)
        assert session._buffer_token != token and not session.selection_active
        assert replacement.raw_range(0, 1) == files.data
    finally:
        hub.close()


def test_real_controller_frames_and_keyboard_never_wait_for_remote_io(tmp_path, monkeypatch):
    from raven import clipboard
    from raven.config import Config
    from raven.controller import App
    from raven.layout import Glyphs, row_text
    from raven.model import Finished, Store
    from raven.views import Views
    files = Remote()
    files.block = True
    cfg = Config({"log_lines": 0, "clipboard": {"osc52": False, "tools": False}})
    store = Store(state_dir=str(tmp_path / "private state"))
    store.finished = [Finished("77", "remote experiment", "FAILED", workdir="/remote/project")]
    store.details["77"] = {"StdOut": "/remote/project/77.out", "WorkDir": "/remote/project"}
    app = App(store, None, None, cfg, "test", ascii_=False)
    app.files, app.logs.files = files, files
    app.research = ResearchHub(cfg, files)
    views = Views(Glyphs(False), cfg, files=files)
    app.views_ref = views
    copied = []
    monkeypatch.setattr(clipboard, "copy", lambda text, state_dir=None, **kw: copied.append(text.encode()) or "copied raw snapshot")
    try:
        app.open_log("77")
        rows, hits = views.compose(store.snapshot(), app, 100, 24)
        assert "background" in "\n".join(row_text(row) for row in rows).lower()
        assert files.entered.wait(1)
        future = app.research.future
        before = time.perf_counter()
        for _ in range(50):
            app.handle("down")
            app.handle("v")
            app.tick()
            views.compose(store.snapshot(), app, 100, 24)
        assert time.perf_counter() - before < .5
        assert app.research.future is future
        assert not app.logs.selection_active
        files.release.set()
        finish(app.research)
        app.tick()
        views.compose(store.snapshot(), app, 100, 24)
        app.handle("home")
        app.handle("v")
        app.handle("down")
        app.handle("y")
        assert copied == [b"first\tcolumn\r\nsecond\n"]
        assert all(name.startswith("raven-research") for _, name in files.calls)
        assert len(files.calls) == 3
    finally:
        files.release.set()
        app.research.close()


def test_unexpected_worker_exception_remains_readable_in_published_buffer():
    files = Remote()
    class Worker:
        def start_task(self, function, completion):
            self.completion = completion
            return True
    worker, session = Worker(), LogSession(files=files)
    session.buffer('remote.log', worker=worker, background=True)
    worker.completion(RuntimeError('remote transport stopped unexpectedly'))
    current = session.buffer('remote.log', worker=worker, background=True)
    assert not current.loading
    assert current.error == 'remote transport stopped unexpectedly'
    assert not session.begin_selection(current)


def test_same_size_remote_rewrite_reloads_and_invalidates_selection():
    files = Remote(b'first\n')
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        original = load(session, hub)
        assert session.begin_selection(original, 0)
        files.data = b'other\n'
        files.updated += 1
        session.invalidate_remote()
        latest = load(session, hub)
        assert latest.raw_range(0, 0) == b'other\n'
        assert original.raw_range(0, 0) == b'first\n'
        assert latest.ident == original.ident and latest.size == original.size
        assert latest.reloads == original.reloads + 1
        assert not session.selection_active and session.cursor is None
        assert all(thread.startswith('raven-research') for operation, thread in files.calls)
    finally: hub.close()


def test_remote_same_size_reload_race_preserves_previous_complete_bytes():
    files = Remote(b'first\n')
    hub, session = ResearchHub({}, files), LogSession(files=files)
    try:
        original = load(session, hub)
        files.data = b'other\n'
        files.updated += 1
        files.after_read = lambda: setattr(files, 'updated', files.updated + 1)
        session.invalidate_remote()
        rejected = load(session, hub)
        assert rejected.error and 'changed during inspection' in rejected.error
        assert rejected.raw_range(0, 0) == b''  # Errored snapshots cannot be copied.
        assert rejected.raw_lines == original.raw_lines
        assert rejected.source_updated == original.source_updated
    finally: hub.close()
