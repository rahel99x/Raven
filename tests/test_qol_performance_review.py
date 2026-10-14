"""Independent frame-path checks: published data, bounded buffers and no I/O.

These tests observe rendered frames and source identities. They do not depend
on wall-clock timing, which can vary on shared cluster and CI hosts.
"""
from __future__ import annotations

import builtins
from collections import deque
import copy
import curses
from pathlib import Path
from types import SimpleNamespace
import subprocess
import threading

import pytest

from raven import analysis_ui, layout as L, log_copy, log_scan, log_tools, navigation_tools, screen, session_tools, table_sort, table_tools
from raven.logs import LogBuffer
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Health, Job, Live, Node, Partition, Store
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"log_lines": 0, "animations": False, "clipboard": {"tools": False, "osc52": False}})
    store = Store(persist=False)
    store.jobs = [Job(str(i), f"job {i}", "main", "RUNNING", cpus=4, elapsed="00:10:00", user="reviewer", hosts=["n1"])
                  for i in range(500)]
    store.live = {job.id: Live(rss=0, avg=.25) for job in store.jobs}
    store.finished = [Finished(str(1000 + i), f"history {i}", "FAILED" if i % 2 else "COMPLETED", cpus=4,
                               end=f"2026-10-05T12:{i % 60:02}:00") for i in range(700)]
    store.group = list(store.jobs)
    store.health = {f"source{i:03}": Health(f"source{i:03}", last_ok=10, errors=i) for i in range(100)}
    store.nodes = {"n1": Node("n1", "MIXED", cpus=64, alloc=32, mem_total=65536, mem_free=0)}
    store.partitions = [Partition("main", "up", nodes=1)]
    application = App(store, None, None, cfg, "reviewer")
    views = Views(L.Glyphs(False), cfg)
    application.views_ref = views
    views.compose(store.snapshot(), application, 120, 32)
    application.selected_id = "0"
    application.analysis_result = {"path": "/published/metrics.jsonl", "series": {
        f"m{i}": [{"t": t, "value": None if t % 31 == 0 else (1e308 if t % 89 == 0 else float(t))}
                  for t in range(1000)] for i in range(64)}}
    application.analysis_result_job = "0"
    application.analysis_result_generation = None
    yield store, application, views
    if application.research:
        application.research.close()


def deny_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A rendered frame attempted file or process I/O")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(LocalFiles, "stat", forbidden)
    monkeypatch.setattr(LocalFiles, "read", forbidden)


@pytest.mark.parametrize("mode", ["exports", "export_preview", "analysis", "project_preview", "log_tools_page"])
def test_opaque_overlays_do_not_admit_hidden_log_tail_reads(dashboard, monkeypatch, mode):
    _, app, _ = dashboard
    path = "/published/stdout.log"
    cached = LogBuffer(path, files=LocalFiles())
    app.logs.buffers[path] = cached
    app.logs.path = path
    app.mode = mode
    def forbidden(*args, **kwargs):
        pytest.fail("Hidden log refresh competed with the explicit overlay operation")
    monkeypatch.setattr(app.logs, "buffer", forbidden)
    for _ in range(3):
        assert app.read_log_buffer(path) is cached


def check_overlay(rows, width=120, height=32):
    assert rows
    assert len(rows) <= height
    assert all(0 <= y < height and 0 <= x < width and x + L.vlen(L.row_text(row)) <= width
               for y, x, row in rows)


def test_frozen_frames_reuse_snapshot_without_copying_or_reading(dashboard, monkeypatch):
    store, app, views = dashboard
    assert table_tools.run_command(app, ["freeze", "on"])
    frozen = app.table_tools_state["freeze"]
    live = store.snapshot()
    live["jobs"] = [Job("new", "a later job", "main", "RUNNING", cpus=8)]
    app.tab = "jobs"
    deny_io(monkeypatch)
    monkeypatch.setattr(copy, "deepcopy", lambda *args, **kwargs: pytest.fail("Frozen frame made a deep copy"))
    for _ in range(20):
        rows, hits = views.compose(live, app, 120, 32)
        assert app.table_tools_state["freeze"] is frozen
        assert "new" not in app.visible_ids and "0" in app.visible_ids
        assert len(rows) == 32 and len(app.last_rows) == 31
        # The menus add only three fixed controls per visible listing.
        additions = {f"joblist:{context}:{section}" for context in ("jobs", "recent")
                     for section in ("view", "order", "groups")}
        extra = [value["id"] for _, kind, value in hits
                 if kind == "control" and value.get("id") in additions]
        assert len(extra) == len(set(extra))
        assert len(hits) - len(extra) <= 50
    assert "queue changes" in app.freeze_label


@pytest.mark.parametrize("tab", ["jobs", "history", "cluster", "nodes", "sources", "group", "deps"])
def test_large_main_tables_use_published_snapshots_and_viewport_storage(dashboard, monkeypatch, tab):
    store, app, views = dashboard
    snap = store.snapshot()
    app.tab = tab
    deny_io(monkeypatch)
    for _ in range(5):
        rows, hits = views.compose(snap, app, 120, 32)
        assert len(rows) == 32 and len(app.last_rows) <= 31
        assert all(L.vlen(L.row_text(row)) <= 120 for row in rows)
        assert all(0 <= y < 31 for y, _, _ in hits)
    assert len(app.last_hits) <= 100


@pytest.mark.parametrize("kind", ["headers", "inbox", "chart", "jump"])
def test_new_overlays_do_not_read_or_copy_a_full_snapshot_each_frame(dashboard, monkeypatch, kind):
    store, app, views = dashboard
    if kind == "headers":
        assert table_tools.run_command(app, ["headers", "jobs"])
        renderer = table_tools.overlay
    elif kind == "inbox":
        assert session_tools.run_command(app, ["inbox"])
        renderer = session_tools.overlay
    elif kind == "chart":
        assert analysis_ui.run_command(app, ["chart", "m0"])
        assert analysis_ui.run_command(app, ["chart", "range", "1", "1000"])
        renderer = analysis_ui.overlay
    else:
        assert navigation_tools.run_command(app, ["jump"])
        navigation_tools._index(app)
        renderer = navigation_tools.overlay
    snap = store.snapshot()
    deny_io(monkeypatch)
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Overlay copied a full snapshot instead of using published data"))
    monkeypatch.setattr(copy, "deepcopy", lambda *args, **kwargs: pytest.fail("Overlay made an unbounded deep copy"))
    for _ in range(10):
        check_overlay(renderer(views, snap, app, 120, 32))
    if kind == "inbox":
        assert len(app.session_tools_state["records"]) <= session_tools.LIMIT
    if kind == "jump":
        assert len(app.navigation_tools_state["index"]) <= navigation_tools.MAX_INDEX


def test_inbox_rotating_published_history_retains_bounded_records(dashboard, monkeypatch):
    store, app, views = dashboard
    app.mode = "session_inbox"
    deny_io(monkeypatch)
    for generation in range(12):
        snap = {"finished": [Finished(f"{generation}_{i}", f"run {i}", "COMPLETED", end=f"2026-10-{generation + 1:02}T12:00:00")
                             for i in range(500)]}
        check_overlay(session_tools.overlay(views, snap, app, 120, 32))
        assert len(app.session_tools_state["records"]) <= session_tools.LIMIT
        assert len(session_tools.inbox_items(app)) <= session_tools.LIMIT


def test_chart_sparse_unknown_interval_never_becomes_continuous_after_zoom(dashboard, monkeypatch):
    store, app, views = dashboard
    app.analysis_result["series"]["outage"] = [{"t": t, "value": value} for t, value in
        [(0, 0), (1, 10), (2, None), (999, 100), (1000, 0)]]
    analysis_ui.run_command(app, ["chart", "outage"])
    analysis_ui.run_command(app, ["chart", "range", "1", "5"])
    snap = store.snapshot()
    deny_io(monkeypatch)
    rows = analysis_ui.overlay(views, snap, app, 120, 32)
    rendered = "\n".join(L.row_text(row) for _, _, row in rows)
    assert "4/5 known samples" in rendered and "missing 1" in rendered
    assert "observed time 0.2%" in rendered
    analysis_ui.handle_key(app, "+")
    rows = analysis_ui.overlay(views, snap, app, 120, 32)
    rendered = "\n".join(L.row_text(row) for _, _, row in rows)
    assert "missing 1" in rendered and "no gap filling" in rendered


class QueuedInput:
    def __init__(self, values):
        self.values = deque(values)

    def timeout(self, value):
        pass

    def get_wch(self):
        if not self.values:
            raise curses.error("No queued input")
        return self.values.popleft()


def test_dense_chart_navigation_batch_reuses_samples_and_defers_event_picker(dashboard, monkeypatch):
    store, app, views = dashboard
    analysis_ui.run_command(app, ["chart", "m0"])
    analysis_ui.overlay(views, store.snapshot(), app, 120, 32)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = QueuedInput([curses.KEY_DOWN] * 12 + [curses.KEY_UP] * 5 + ["e", curses.KEY_DOWN])
    with monkeypatch.context() as guarded:
        guarded.setattr(store, "snapshot", lambda: pytest.fail("Each sample move copied a full snapshot"))
        guarded.setattr(analysis_ui, "_points", lambda *args: pytest.fail("Each sample move normalized the whole series"))
        pending = screen._consume_input_batch(app, window, curses, (), ("down", None))
    assert app.analysis_state["cursor"] == 8 and app.analysis_state["modal"] == "chart"
    assert pending == ("e", None) and list(window.values) == [curses.KEY_DOWN]
    analysis_ui.overlay(views, store.snapshot(), app, 120, 32)
    screen._consume_input_batch(app, window, curses, (), pending)
    assert app.analysis_state["modal"] == "chart_events" and app.analysis_state["sample_cursor"] == 8
    assert list(window.values) == [curses.KEY_DOWN]


def test_inbox_navigation_batch_reviews_exact_final_completion_after_fresh_frame(dashboard, monkeypatch):
    store, app, views = dashboard
    session_tools.run_command(app, ["inbox"])
    session_tools.overlay(views, store.snapshot(), app, 120, 32)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = QueuedInput([curses.KEY_DOWN] * 12 + [curses.KEY_UP] * 5 + ["r", curses.KEY_DOWN])
    pending = screen._consume_input_batch(app, window, curses, (), ("down", None))
    items = session_tools.inbox_items(app)
    target = items[8]["key"]
    assert app.session_tools_state["cursor"] == 8 and app.session_tools_state["selected"] == target
    assert target not in app.session_tools_state["reviewed"] and pending == ("r", None)
    session_tools.overlay(views, store.snapshot(), app, 120, 32)
    screen._consume_input_batch(app, window, curses, (), pending)
    assert target in app.session_tools_state["reviewed"]
    assert list(window.values) == [curses.KEY_DOWN]


def test_header_navigation_batch_sorts_final_column_without_consuming_next_move(dashboard, monkeypatch):
    store, app, views = dashboard
    table_tools.run_command(app, ["headers", "jobs"])
    table_tools.overlay(views, store.snapshot(), app, 120, 32)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = QueuedInput([curses.KEY_DOWN] * 5 + [curses.KEY_UP] * 2 + ["\n", curses.KEY_DOWN])
    pending = screen._consume_input_batch(app, window, curses, (), ("down", None))
    assert app.table_tools_state["cursor"] == 4 and pending == ("enter", None)
    assert table_sort.chain(app, "jobs") is None
    table_tools.overlay(views, store.snapshot(), app, 120, 32)
    target = app.table_tools_state["header"]["column"]
    screen._consume_input_batch(app, window, curses, (), pending)
    assert table_sort.chain(app, "jobs") == [(target, "asc")]
    assert list(window.values) == [curses.KEY_DOWN]


def test_source_page_batch_preserves_raw_selection_and_exact_copy_source(dashboard, monkeypatch):
    store, app, views = dashboard
    raw = [f"line_{i:03}\tUnicode 界\r\n".encode() for i in range(100)]
    app.tab, app.mode, app.log_job = "log", "log_tools_page", "0"
    app.logs.path = "/currently-running.out"
    state = log_tools.initialize(app)
    source = {"path": "/exact/job-0/worker-3.stderr", "target": "local", "label": "Worker stderr"}
    state["page_source"] = source
    state["page"] = {"path": source["path"], "start": 0, "end": sum(map(len, raw)), "size": sum(map(len, raw)),
        "snapshot": {"ident": (1, 2)}, "complete": True, "partial": False,
        "rows": [{"raw": value, "text": value.decode().rstrip("\r\n"), "line": i + 1, "offset": i * 20}
                 for i, value in enumerate(raw)]}
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = QueuedInput([curses.KEY_DOWN] * 5 + ["v"])
    pending = screen._consume_input_batch(app, window, curses, (), ("down", None))
    assert state["page_cursor"] == 6 and pending == ("v", None) and state["selection"] is None
    log_tools.overlay(views, store.snapshot(), app, 120, 32)
    screen._consume_input_batch(app, window, curses, (), pending)
    assert state["selection"] == (6, 6)
    window = QueuedInput([curses.KEY_DOWN] * 2 + [curses.KEY_UP, "y", curses.KEY_DOWN])
    pending = screen._consume_input_batch(app, window, curses, (), ("down", None))
    assert state["selection"] == (6, 8) and pending == ("y", None)
    captured = []
    def capture(chunks, directory=None, **kwargs):
        captured.append((b"".join(chunks), kwargs["source_path"]))
        return {"status": "ready", "message": "Captured exact bytes"}
    monkeypatch.setattr(log_copy, "copy_log_selection", capture)
    monkeypatch.setattr(log_tools, "_task", lambda application, label, fn, complete, **kwargs: complete(fn(None, None)))
    log_tools.overlay(views, store.snapshot(), app, 120, 32)
    screen._consume_input_batch(app, window, curses, (), pending)
    assert captured == [(b"".join(raw[6:9]), source["path"])] and state["selection"] is None
    assert app.logs.path == "/currently-running.out" and list(window.values) == [curses.KEY_DOWN]


def test_same_file_page_replacement_is_a_navigation_batch_barrier(monkeypatch):
    app = SimpleNamespace(mode="log_tools_page", tab="log", quit=False,
        log_tools_state={"page": {"path": "/same/source", "start": 0, "end": 100, "snapshot": {"ident": (1, 2)}}})
    seen = []
    def handle(key):
        seen.append(key)
        app.log_tools_state["page"]["start"] = 100
    app.handle = handle
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = QueuedInput([curses.KEY_DOWN, "y"])
    assert screen._consume_input_batch(app, window, curses, (), ("down", None)) is None
    assert seen == ["down"] and list(window.values) == [curses.KEY_DOWN, "y"]


def test_large_unicode_log_page_pans_are_bounded_cached_and_do_not_modify_raw_bytes(dashboard, monkeypatch):
    store, app, views = dashboard
    text = "界" * 80000
    original = text.encode() + b"\r\n"
    app.mode = "log_tools_page"
    state = log_tools.initialize(app)
    state["page_source"] = {"path": "/exact/worker.stderr", "label": "Worker stderr"}
    page = {"path": "/exact/worker.stderr", "start": 0, "end": len(original), "size": len(original),
            "complete": True, "partial": False, "rows": [{"text": text, "raw": original, "offset": 0, "line": 1}]}
    state.update(page=page, page_pan=100000)
    calls = []
    original_pan = log_tools._pan
    def capture(*args, **kwargs):
        value = original_pan(*args, **kwargs)
        calls.append(value)
        return value
    monkeypatch.setattr(log_tools, "_pan", capture)
    deny_io(monkeypatch)
    for _ in range(10):
        check_overlay(log_tools.overlay(views, {}, app, 120, 32))
    assert len(calls) == 1 and len(calls[0]) <= 960
    assert page["rows"][0]["raw"] is original and page["rows"][0]["text"] is text
    # Each change stores only a viewport excerpt, and the cache has a fixed cap.
    page = {**page, "rows": [{"text": "x" * 240000, "raw": b"x" * 240000, "offset": 0, "line": 1}]}
    state["page"] = page
    for offset in range(200):
        state["page_pan"] = offset * 8
        check_overlay(log_tools.overlay(views, {}, app, 120, 32))
    assert len(state["page_pan_cache"]) <= 128
    assert all(len(value) <= 960 for value in state["page_pan_cache"].values())


def test_cold_jump_index_bounds_inventory_iteration_and_uses_no_full_snapshot(dashboard, monkeypatch):
    store, app, views = dashboard
    class LargeInventory:
        observed = 0
        def __iter__(self):
            for index in range(1000000):
                self.observed += 1
                if self.observed > navigation_tools.MAX_INDEX:
                    pytest.fail("Jump indexing walked the entire inventory before applying its bound")
                yield Job(str(index), f"large inventory job {index}", "main", "RUNNING")
    inventory = LargeInventory()
    store.jobs, store.finished = inventory, []
    app.mode = "jump_picker"
    app.navigation_tools_state["index_at"] = -1
    deny_io(monkeypatch)
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Jump overlay copied a full Store snapshot"))
    monkeypatch.setattr(navigation_tools.time, "monotonic", lambda: 10)
    check_overlay(navigation_tools.overlay(views, {}, app, 120, 32))
    assert inventory.observed <= navigation_tools.MAX_INDEX
    assert len(app.navigation_tools_state["index"]) <= navigation_tools.MAX_INDEX


def test_chart_resource_history_reads_only_bounded_tail(dashboard):
    store, app, _ = dashboard
    class LongHistory:
        observed = 0
        def __reversed__(self):
            for index in reversed(range(1000000)):
                self.observed += 1
                if self.observed > analysis_ui.MAX_POINTS:
                    pytest.fail("Chart copied the complete resource history before taking its tail")
                yield {"t": index, "value": index}
    history = LongHistory()
    store.series["0"] = history
    result = analysis_ui.memory_series(app, "0")
    assert history.observed == len(result) == analysis_ui.MAX_POINTS
    assert result[0]["t"] == 1000000 - analysis_ui.MAX_POINTS and result[-1]["t"] == 999999


class AppendingSource:
    """A regular source model; no operating-system file is used by these tests."""
    min_refresh = 0
    remote = False
    def __init__(self, raw):
        self.raw, self.ident = raw, (1, 1)

    def stat(self, path):
        return len(self.raw), self.ident

    def read(self, path, offset, length):
        return self.raw[offset:offset + length]


@pytest.fixture
def interactive_retained_tail(monkeypatch):
    """Exactly 200,000 completed lines with real matches and a partial EOF."""
    normal = "normal " + "x" * 143
    matching = "MATCH " + "x" * 144
    lines = [normal] * 200000
    for index in (3, 1000, 199999):
        lines[index] = matching
    raw_lines = [line.encode() for line in lines]
    source = AppendingSource(b"\n".join(raw_lines) + b"\nMAT")
    path = "/published/exact.stderr"
    buf = LogBuffer(path, files=source)
    buf.lines, buf.raw_lines = lines, raw_lines
    buf._line_bytes = [151] * 200000
    buf._partial_raw, buf.partial = b"MAT", "MAT"
    buf._retained_bytes = buf.size = len(source.raw)
    buf.ident = source.ident
    cfg = Config({"log_lines": 0, "animations": False, "clipboard": {"tools": False, "osc52": False}})
    store = Store(persist=False)
    store.jobs = [Job("9", "retained search", "main", "RUNNING")]
    store.details["9"] = {"StdOut": path, "StdErr": path}
    app = App(store, None, None, cfg, "reviewer", interactive=True)
    views = Views(L.Glyphs(False), cfg)
    app.tab, app.log_job, app.selected_id, app.views_ref = "log", "9", "9", views
    app.logs.entry = {"id": "stderr", "path": path, "label": "stderr"}
    app.logs.path, app.logs.search = path, "MATCH"
    app.logs.sync_buffer(buf)
    latest = [buf]
    app.read_log_buffer = lambda selected: latest[0]
    state = log_tools.initialize(app)
    state["search_mode"] = {"regex": True, "case": False, "word": False}
    state["retained_explicit"] = True
    app.research = ResearchHub(cfg, source)
    main_thread = threading.get_ident()
    scans = {"foreground": 0, "worker": 0}
    compile_search = log_scan.compile_search
    class ObservedMatcher:
        def __init__(self, delegate): self.delegate = delegate
        def search(self, *args, **kwargs):
            field = "foreground" if threading.get_ident() == main_thread else "worker"
            scans[field] += 1
            assert field != "foreground" or scans[field] <= 4096, "Interactive search scanned the whole retained tail on the terminal thread"
            return self.delegate.search(*args, **kwargs)
        def __getattr__(self, name): return getattr(self.delegate, name)
    monkeypatch.setattr(log_scan, "compile_search", lambda *args, **kwargs: ObservedMatcher(compile_search(*args, **kwargs)))
    original_all_lines = LogBuffer.all_lines
    def all_lines(buffer):
        assert threading.get_ident() != main_thread, "Terminal operation copied all 200,000 retained lines"
        return original_all_lines(buffer)
    monkeypatch.setattr(LogBuffer, "all_lines", all_lines)
    gate, admissions = threading.Event(), []
    start_task = app.research.start_task
    def start(fn, callback):
        def gated():
            assert gate.wait(10), "Test did not release the admitted search worker"
            return fn()
        accepted = start_task(gated, callback)
        admissions.append(accepted)
        return accepted
    monkeypatch.setattr(app.research, "start_task", start)
    def publish():
        gate.set()
        assert app.research.pending is not None, "The index was never submitted to the worker"
        app.research.pending[0].result(timeout=10)
        app.research.poll_task()
    yield source, latest, app, views, state, scans, admissions, gate, publish
    gate.set()
    if app.research.pending:
        app.research.pending[0].result(timeout=10)
        app.research.poll_task()
    app.research.close()


def test_interactive_first_query_frame_and_next_admit_real_index_without_scanning_tail(interactive_retained_tail):
    source, latest, app, views, state, scans, admissions, gate, publish = interactive_retained_tail
    buf = latest[0]
    assert log_tools.count_retained(app, buf) == 0
    assert state["retained_pending"] and state["retained_known_count"] is None
    assert admissions == [True] and scans == {"foreground": 0, "worker": 0}
    rows, _ = views.compose(app.store.snapshot(), app, 120, 32)
    rendered = "\n".join(L.row_text(row) for row in rows)
    assert "indexing" in rendered.lower()
    assert state["retained_total"] == 200001 and state["retained_processed"] == 0
    assert log_tools.find_retained(app, buf) == (True, None)
    assert state["retained_pending"] and admissions == [True]
    assert scans["foreground"] <= 32 and scans["worker"] == 0
    publish()
    assert log_tools.count_retained(app, buf) == 3 and not state["retained_pending"]
    assert state["retained_processed"] == 200001 and scans["worker"] == 200001
    foreground = scans["foreground"]
    app.logs.cursor, app.logs.match = 0, None
    assert log_tools.find_retained(app, buf) == (True, 3)
    assert log_tools.find_retained(app, buf) == (True, 1000)
    assert log_tools.find_retained(app, buf, backwards=True) == (True, 3)
    assert scans["foreground"] == foreground
    # A missing query must actually finish indexing before a zero is known.
    gate.clear()
    app.logs.search = "MISSING"
    assert log_tools.find_retained(app, buf) == (True, None)
    assert state["retained_pending"] and state["retained_known_count"] is None and admissions == [True, True]
    assert log_tools.find_retained(app, buf) == (True, None) and admissions == [True, True]
    publish()
    assert log_tools.count_retained(app, buf) == 0 and state["retained_known_count"] == 0
    assert not state["retained_pending"] and state["retained_processed"] == 200001
    assert scans["worker"] == 400002
    before = dict(scans)
    for _ in range(10):
        assert log_tools.find_retained(app, buf) == (True, None)
    assert scans == before and admissions == [True, True]


def test_interactive_append_partial_and_trim_scan_only_new_rows_then_rebuild_replacement(interactive_retained_tail):
    source, latest, app, views, state, scans, admissions, gate, publish = interactive_retained_tail
    log_tools.count_retained(app, latest[0])
    publish()
    assert state["retained_known_count"] == 3
    scans["foreground"] = 0
    expected_counts = (4, 5, 5, 5)
    for suffix, expected in zip((b"CH\n", b"MATCH partial", b" suffix", b"\n"), expected_counts):
        source.raw += suffix
        updated = latest[0]._worker_copy()
        updated.refresh()
        latest[0] = updated
        assert len(updated.lines) == 200000
        before = scans["foreground"]
        assert log_tools.count_retained(app, updated) == expected
        assert scans["foreground"] - before <= 2 and admissions == [True]
        assert not state["retained_pending"] and state["retained_processed"] == updated.total
        assert updated.skipped_bytes > 0
        before = scans["foreground"]
        rows, _ = views.compose(app.store.snapshot(), app, 120, 32)
        assert len(rows) == 32 and scans["foreground"] - before <= 32
    # A new inode must not inherit any count or positions from the former file.
    source.ident = (2, 3)
    source.raw = b"MATCH replacement\n" + b"normal\n" * 199999
    replacement = latest[0]._worker_copy()
    replacement.refresh()
    latest[0] = replacement
    gate.clear()
    assert log_tools.count_retained(app, replacement) == 0
    assert state["retained_pending"] and state["retained_known_count"] is None
    assert admissions == [True, True]
    publish()
    assert log_tools.count_retained(app, replacement) == 1
    app.logs.cursor, app.logs.match = None, None
    assert log_tools.find_retained(app, replacement) == (True, 0)
    assert state["retained_processed"] == 200000 and not state["retained_pending"]
