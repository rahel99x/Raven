from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import layout as L
from raven import log_workbench as workbench
from raven.logs import LogBuffer, LogSession
from raven.model import Finished
from raven.remote import LocalFiles
from raven.research import ResearchHub


def app_for(files=None):
    files = files or LocalFiles()
    logs = LogSession(files=files)
    app = SimpleNamespace(logs=logs, files=files, tab="log", mode="main",
                          research=ResearchHub({}, files), message="", command_ok=True,
                          selected_id="77", research_job_id="77", log_selection_expected=False)
    def say(value):
        app.message = value
    def fail(value):
        app.command_ok = False
        app.message = value
    app.say, app.fail = say, fail
    app.log_entries = lambda: workbench.visible_entries(app, logs.entries)
    workbench.initialize(app)
    return app


def complete(app):
    app.research.future.result(timeout=5)
    app.research.poll_task()


def rows_text(rows):
    return "\n".join(L.row_text(row) for _, _, row in rows)


def test_saved_preferences_exclude_worker_state_source_paths_and_horizontal_position():
    app = app_for()
    try:
        workbench.restore(app, {"view": "split", "pan": 24, "collapsed": ["Workers"], "preview": True,
                                "cache": {"secret": "ignored"}})
        assert workbench.save(app) == {"view": "split", "collapsed": ["Workers"], "preview": True}
        assert app.log_workbench_state["pan"] == 0
        assert app.log_workbench_state["cache"] == {}
        json.dumps(workbench.save(app))
    finally:
        app.research.close()


@pytest.mark.parametrize("state", [None, [], {"pan": True}, {"pan": -100}, {"view": "unknown"}, {"collapsed": ["\x1bsecret", "a" * 161, 3]}])
def test_malformed_preferences_are_bounded(state):
    app = app_for()
    try:
        workbench.restore(app, state)
        assert app.log_workbench_state["view"] == "plain"
        assert app.log_workbench_state["pan"] == 0
        assert app.log_workbench_state["collapsed"] == []
    finally:
        app.research.close()


def test_unicode_horizontal_pan_never_splits_wide_glyph_and_does_not_change_raw_selection(tmp_path):
    path = tmp_path / "77.out"
    raw = "ab界\tvalue\r\nsecond\n".encode()
    path.write_bytes(raw)
    app = app_for()
    try:
        app.logs.path = str(path)
        buf = app.logs.buffer(str(path))
        app.logs.begin_selection(buf, 0)
        app.logs.move_cursor("end", buf)
        before = app.logs.selection_bytes(buf)
        assert workbench.run_command(app, ["logpan", "3"])
        assert workbench.display_line(app, "ab界value") == "value"
        assert app.logs.selection_bytes(buf) == before == raw
        assert workbench.handle_key(app, "left")
        assert workbench.display_line(app, "ab界value") == "ab界value"
    finally:
        app.research.close()


def test_groups_collapse_expand_preserving_catalog_identities(tmp_path):
    app = app_for()
    app.logs.entries = [dict(id="a", path="/tmp/a", label="A", group="Workers"),
                        dict(id="b", path="/tmp/b", label="B", group="Scheduler")]
    app.logs.browser = True
    try:
        assert workbench.handle_key(app, "space")
        assert [entry["id"] for entry in app.log_entries()] == ["b"]
        assert len(app.logs.entries) == 2
        workbench.run_command(app, ["loggroup", "Workers"])
        assert [entry["id"] for entry in app.log_entries()] == ["a", "b"]
        workbench.run_command(app, ["loggroup", "all"])
        assert not app.log_workbench_state["collapsed"]
    finally:
        app.research.close()


def test_render_metadata_and_preview_reads_worker_only_and_reuses_snapshot(tmp_path):
    path = tmp_path / "77.out"
    path.write_bytes(b"one\ntwo\nthree\n")
    class Files(LocalFiles):
        def __init__(self):
            self.threads = []
        def snapshot_stat(self, path):
            import threading
            self.threads.append(threading.current_thread().name)
            return super().snapshot_stat(path)
    files = Files()
    app = app_for(files)
    app.logs.entries = [dict(id="a", path=str(path), label="stdout", group="Scheduler")]
    app.logs.browser = True
    app.log_workbench_state["preview"] = True
    views = SimpleNamespace(g=L.Glyphs(False))
    try:
        rows, hits = workbench.render_browser(views, {}, app, 80, 20, [], [])
        complete(app)
        count = len(files.threads)
        for _ in range(20):
            rows, hits = workbench.render_browser(views, {}, app, 80, 20, [], [])
        assert count == len(files.threads)
        assert all(name.startswith("raven-research") for name in files.threads)
        text = "\n".join(L.row_text(row) for row in rows)
        assert "14 B" in text and "three" in text
        assert any(kind == "log_group" for _, kind, _ in hits)
        assert any(kind == "log_file" and key == "a" for _, kind, key in hits)
        assert all(L.vlen(L.row_text(row)) <= 80 for row in rows)
    finally:
        app.research.close()


def test_json_view_formats_bounded_snapshot_and_never_modifies_copy_bytes(tmp_path):
    path = tmp_path / "77.jsonl"
    raw = b'{"loss": 0.5, "phase": "train"}\r\n{"ok": true}\n'
    path.write_bytes(raw)
    app = app_for()
    app.logs.path = str(path)
    buf = app.logs.buffer(str(path))
    app.logs.begin_selection(buf, 0)
    app.logs.move_cursor("end", buf)
    views = SimpleNamespace(g=L.Glyphs(False))
    try:
        workbench.run_command(app, ["logview", "json"])
        assert workbench.overlay(views, {}, app, 90, 24) is not None
        complete(app)
        text = rows_text(workbench.overlay(views, {}, app, 90, 24))
        assert '"loss": 0.5' in text and "L1" in text
        assert app.logs.selection_bytes(buf) == raw
        assert workbench.handle_key(app, "esc")
        assert workbench.overlay(views, {}, app, 90, 24) is None
    finally:
        app.research.close()


@pytest.mark.parametrize("width,height", [(1, 1), (3, 2), (8, 5), (40, 12), (160, 50)])
@pytest.mark.parametrize("ascii_", [False, True])
def test_alternate_view_resizes_and_ascii(tmp_path, width, height, ascii_):
    path = tmp_path / "77.jsonl"
    path.write_bytes(b'{"phase":"train"}\n')
    app = app_for()
    app.logs.path = str(path)
    views = SimpleNamespace(g=L.Glyphs(ascii_))
    try:
        workbench.run_command(app, ["logview", "json"])
        workbench.overlay(views, {}, app, width, height)
        complete(app)
        rows = workbench.overlay(views, {}, app, width, height)
        assert all(0 <= x < width and 0 <= y < height and x + L.vlen(L.row_text(row)) <= width for y, x, row in rows)
        if ascii_:
            assert rows_text(rows).isascii()
    finally:
        app.research.close()


def test_split_view_uses_two_explicit_registered_sources_and_copy_focus(tmp_path):
    out, err = tmp_path / "77.out", tmp_path / "77.err"
    out.write_bytes(b"progress\n")
    err.write_bytes(b"CUDA out of memory\n")
    app = app_for()
    app.logs.entries = [dict(id="scheduler.stdout", path=str(out), label="stdout", group="Scheduler"),
                        dict(id="scheduler.stderr", path=str(err), label="stderr", group="Scheduler")]
    app.logs.path = str(out)
    views = SimpleNamespace(g=L.Glyphs(False))
    try:
        workbench.run_command(app, ["logview", "split"])
        workbench.overlay(views, {}, app, 100, 24)
        complete(app)
        text = rows_text(workbench.overlay(views, {}, app, 100, 24))
        assert "progress" in text and "CUDA out of memory" in text
        assert workbench.handle_key(app, "]")
        assert app.logs.entry["path"] == str(err)
        assert workbench.handle_key(app, "[")
        assert app.logs.entry["path"] == str(out)
    finally:
        app.research.close()


def test_remote_reader_failure_never_uses_existing_local_file(tmp_path):
    path = tmp_path / "77.jsonl"
    path.write_bytes(b'{"secret": "local only"}\n')
    class Remote:
        remote = True
        min_refresh = 0
        def snapshot_stat(self, path):
            raise OSError("SSH unavailable")
        def tail(self, path, count):
            pytest.fail("Metadata failure must not trigger fallback")
    app = app_for(Remote())
    app.logs.path = str(path)
    views = SimpleNamespace(g=L.Glyphs(False))
    try:
        workbench.run_command(app, ["logview", "json"])
        workbench.overlay(views, {}, app, 100, 20)
        complete(app)
        text = rows_text(workbench.overlay(views, {}, app, 100, 20))
        assert "SSH unavailable" in text and "local only" not in text
    finally:
        app.research.close()


def test_tail_preview_drops_partial_prefix_and_labels_budget(tmp_path):
    path = tmp_path / "77.out"
    path.write_bytes(b"x" * 100000 + b"\nlast complete line\n")
    raw, size, metadata, truncated = workbench._snapshot_tail(LocalFiles(), str(path), 8192)
    assert raw == b"last complete line\n" and truncated
    assert metadata["size"] == size == path.stat().st_size


def test_file_mutation_during_alternate_read_is_not_published_as_valid(tmp_path):
    path = tmp_path / "77.out"
    path.write_bytes(b"old\n")
    class Mutating(LocalFiles):
        def tail(self, path, count):
            raw, size = super().tail(path, count)
            Path(path).write_bytes(b"new contents\n")
            return raw, size
    app = app_for(Mutating())
    app.logs.path = str(path)
    views = SimpleNamespace(g=L.Glyphs(False))
    try:
        workbench.run_command(app, ["logview", "json"])
        workbench.overlay(views, {}, app, 100, 20)
        complete(app)
        text = rows_text(workbench.overlay(views, {}, app, 100, 20))
        assert "changed during inspection" in text
        assert "L1 old" not in text
    finally:
        app.research.close()


def test_tail_citation_finds_correct_repeated_occurrence(tmp_path):
    path = tmp_path / "77.out"
    path.write_bytes(b"prefix\nRuntimeError: same\ncontext\nRuntimeError: same\nend\n")
    app = app_for()
    try:
        app.logs.path = str(path)
        buf = app.logs.buffer(str(path))
        citation = dict(id="E4", path=str(path), line=2, line_basis="tail-relative", tail_distance=3,
                        excerpt_line="RuntimeError: same", file_identity=LocalFiles().snapshot_stat(str(path)))
        assert workbench.open_citation(app, citation)
        buf = app.logs.buffer(str(path))
        workbench.apply_citation(app, buf)
        assert app.logs.cursor == 1
        assert app.logs.match == 1
    finally:
        app.research.close()


def test_rotated_citation_opens_source_but_never_jumps_to_replacement(tmp_path):
    path = tmp_path / "77.out"
    path.write_bytes(b"old\nRuntimeError: old\n")
    metadata = LocalFiles().snapshot_stat(str(path))
    path.rename(tmp_path / "old.out")
    path.write_bytes(b"RuntimeError: old\n")
    app = app_for()
    try:
        citation = dict(id="E4", path=str(path), line=2, line_basis="original", file_identity=metadata)
        workbench.open_citation(app, citation)
        workbench.apply_citation(app, app.logs.buffer(str(path)))
        assert app.logs.cursor is None
        assert "replaced after inspection" in app.message
    finally:
        app.research.close()


def test_growing_tail_citation_does_not_guess_repeated_occurrence(tmp_path):
    path = tmp_path / "77.out"
    path.write_bytes(b"RuntimeError: same\n")
    metadata = LocalFiles().snapshot_stat(str(path))
    with path.open("ab") as stream:
        stream.write(b"RuntimeError: same\n")
    app = app_for()
    try:
        citation = dict(id="E4", path=str(path), line=1, line_basis="tail-relative", tail_distance=0,
                        excerpt_line="RuntimeError: same", file_identity=metadata)
        workbench.open_citation(app, citation)
        workbench.apply_citation(app, app.logs.buffer(str(path)))
        assert app.logs.cursor is None
        assert "changed after inspection" in app.message
    finally:
        app.research.close()


def test_nonlog_citation_and_invalid_path_never_open_arbitrary_file():
    app = app_for()
    try:
        assert not workbench.open_citation(app, {"id": "E1", "source": "scheduler"})
        assert not workbench.open_citation(app, {"id": "E2", "path": "x\x1b[2J"})
        assert app.logs.path == ""
    finally:
        app.research.close()


def test_busy_worker_never_queues_additional_presentation_jobs(tmp_path):
    from threading import Event
    path = tmp_path / "77.out"
    path.write_bytes(b"test\n")
    app = app_for()
    app.logs.path = str(path)
    release = Event()
    try:
        assert app.research.start_task(lambda: release.wait(2), lambda result: None)
        future = app.research.future
        workbench.run_command(app, ["logview", "json"])
        for _ in range(100):
            workbench.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, 100, 20)
        assert app.research.future is future
        assert app.log_workbench_state["pending"] is None
    finally:
        release.set()
        complete(app)
        app.research.close()


def test_giant_panned_line_allocates_only_bounded_display_and_caches_column_scan():
    app = app_for()
    try:
        line = "a" * 8_000_000
        app.log_workbench_state["pan"] = 100000
        first = workbench.display_line(app, line)
        assert len(first) <= 65536
        cached = app.log_workbench_state["pan_cache"][id(line)]
        assert cached[0] is line and cached[1] == 100000
        for _ in range(10):
            assert workbench.display_line(app, line) == first
        assert len(app.log_workbench_state["pan_cache"]) == 1
    finally:
        app.research.close()


def test_browser_keeps_manifest_errors_visible_with_new_metadata_layout(tmp_path):
    path = tmp_path / "77.out"
    path.write_bytes(b"example\n")
    app = app_for()
    app.logs.entries = [dict(id="a", path=str(path), label="stdout", group="Scheduler")]
    app.logs.browser = True
    views = SimpleNamespace(g=L.Glyphs(False))
    try:
        rows, _ = workbench.render_browser(views, {}, app, 100, 24,
                    [[("title", "")], [("Log manifest unavailable: duplicate JSON key", "yellow")]], [])
        assert "duplicate JSON key" in "\n".join(L.row_text(row) for row in rows)
        complete(app)
    finally:
        app.research.close()


def real_dashboard(tmp_path):
    from raven.config import Config
    from raven.controller import App
    from raven.model import Store
    from raven.views import Views
    out, err = tmp_path / "stdout-77.log", tmp_path / "stderr-77.log"
    out.write_bytes(b"first\twide column\r\nsecond\r\nthird\n")
    err.write_bytes(b"CUDA out of memory\n")
    cfg = Config({"log_lines": 0, "clipboard": {"osc52": False, "tools": False}})
    store = Store(state_dir=str(tmp_path / "private state"))
    store.finished = [Finished("77", "experiment", "FAILED", workdir=str(tmp_path))]
    store.details["77"] = {"StdOut": str(out), "StdErr": str(err), "WorkDir": str(tmp_path)}
    # Presentation/copy semantics use explicit synchronous snapshots; local
    # interactive worker behavior is covered in test_local_log_snapshots.py.
    app = App(store, None, None, cfg, "test", ascii_=False, interactive=False)
    app.files, app.logs.files = LocalFiles(), LocalFiles()
    app.research = ResearchHub(cfg, app.files)
    views = Views(L.Glyphs(False), cfg, files=app.files)
    app.views_ref = views
    app.open_log("77")
    views.compose(store.snapshot(), app, 100, 24)
    return app, views, out, err


def test_real_app_horizontal_pan_keeps_yanked_source_bytes(tmp_path, monkeypatch):
    from raven import clipboard
    app, views, out, err = real_dashboard(tmp_path)
    copied = []
    monkeypatch.setattr(clipboard, "copy", lambda value, state_dir=None, **kwargs: copied.append(value.encode()) or "copied")
    try:
        app.handle("home")
        app.handle("v")
        app.handle("down")
        app.run_command("logpan 8")
        rows, _ = views.compose(app.store.snapshot(), app, 100, 24)
        assert "pan 8" in "\n".join(L.row_text(row) for row in rows)
        app.handle("y")
        assert copied == [b"first\twide column\r\nsecond\r\n"]
    finally:
        app.research.close()


def test_real_app_split_view_resolves_scheduler_pair_without_prior_browser(tmp_path):
    app, views, out, err = real_dashboard(tmp_path)
    try:
        assert not app.logs.entries
        app.run_command("logview split")
        views.compose(app.store.snapshot(), app, 100, 24)
        views.overlay(app.store.snapshot(), app, 100, 24)
        complete(app)
        overlay = views.overlay(app.store.snapshot(), app, 100, 24)
        assert "CUDA out of memory" in rows_text(overlay)
        app.handle("]")
        app.handle("esc")
        assert app.resolve_log_path() == str(err)
        views.compose(app.store.snapshot(), app, 100, 24)
        assert app.logs.path == str(err)
    finally:
        app.research.close()


def test_real_app_follows_proven_citation_job_after_other_selection(tmp_path):
    app, views, out, err = real_dashboard(tmp_path)
    app.store.finished.append(Finished("99", "other", "FAILED", workdir=str(tmp_path)))
    app.selected_id, app.research_job_id = "99", "99"
    try:
        citation = dict(id="E4", job="77", path=str(out), line=2, line_basis="original",
                        file_identity=app.files.snapshot_stat(str(out)))
        assert workbench.open_citation(app, citation)
        views.compose(app.store.snapshot(), app, 100, 24)
        assert app.log_job == "77" and app.logs.path == str(out)
        assert app.logs.cursor == app.logs.match == 1
    finally:
        app.research.close()


@pytest.mark.parametrize("placeholder", [None, "", " ", "(null)", "N/A", "UNKNOWN", "None"])
def test_cached_log_catalog_never_probes_missing_scheduler_paths_on_redraw(monkeypatch, placeholder):
    import os
    from raven.model import Job
    app = app_for()
    catalog = [dict(id="manifest.worker", path="/declared/worker.log", label="Worker", group="Application")]
    app.logs.entries = catalog
    job = Job(id="77", name="training", state="RUNNING", partition="cpu")
    snap = {"jobs": [job], "details": {"77": {"StdOut": placeholder, "StdErr": placeholder}}}
    app.store = SimpleNamespace(snapshot=lambda: snap)
    app.log_target = lambda snapshot: job
    monkeypatch.setattr(os.path, "exists", lambda path: pytest.fail("A cached catalog redraw cannot guess a filesystem path"))
    try:
        for _ in range(100):
            assert workbench._entries(app) == catalog
    finally:
        app.research.close()


def test_known_scheduler_paths_expand_without_filesystem_probes(monkeypatch):
    import os
    from raven.model import Job
    app = app_for()
    job = Job(id="77_2", name="training", state="RUNNING", partition="cpu")
    snap = {"jobs": [job], "details": {job.id: {"StdOut": "logs/%A-%a.out", "StdErr": "(null)", "WorkDir": "/project", "LogPathSource": "sacct"}}}
    app.store = SimpleNamespace(snapshot=lambda: snap)
    app.log_target = lambda snapshot: job
    monkeypatch.setattr(os.path, "exists", lambda path: pytest.fail("Explicit path expansion must stay pure"))
    try:
        entries = workbench._entries(app)
        assert len(entries) == 1
        assert entries[0]["path"] == "/project/logs/77-2.out"
    finally:
        app.research.close()
