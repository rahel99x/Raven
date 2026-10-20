"""Regression evidence for the adversarial UI, logs and PTY review."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from raven import clipboard, historical_replay, layout, log_catalog, log_scan
from raven.hooks import run_hook
from raven.log_text import display_text
from raven.model import Job, Store
from raven.redaction import redact
from raven.remote import LocalFiles
from raven.ui_wakeup import UIWakeup, UIWaiter
from raven.worker_scheduler import WorkerScheduler


@pytest.mark.parametrize(
    "raw",
    [
        "name\x1b]52;c;ZXZpbA==\x07tail",
        "a\x00b\x85c",
        "a\u202eb\u200dc",
        "name\ud800tail",
    ],
)
def test_untrusted_terminal_text_has_consistent_safe_cell_width(raw):
    safe = display_text(raw)
    assert all(c.isprintable() for c in safe)
    assert not any(c in safe for c in "\x1b\x07\x00\x85\u202e\u200d")
    assert layout.vlen(raw) == layout.vlen(safe)
    assert layout.to_text([[(raw, "")]], 1000, color=False) == safe
    assert layout.vlen(layout.cut(raw, 9)) <= 9


def test_discovery_and_reads_refuse_planted_log_symlink(tmp_path):
    target = tmp_path / "private"
    target.write_text("SECRET")
    (tmp_path / "slurm-73.out").symlink_to(target)
    (tmp_path / "job-73.err").write_text("legitimate")
    names, _ = log_catalog._directory_names(LocalFiles(), str(tmp_path))
    assert "slurm-73.out" not in names and "job-73.err" in names
    with pytest.raises(OSError):
        LocalFiles().read(str(tmp_path / "slurm-73.out"), 0, 100)
    assert target.read_text() == "SECRET"


def test_bounded_search_reaches_newest_output_without_inventing_line_numbers(tmp_path):
    path = tmp_path / "large.log"
    path.write_bytes(b"old line\n" * 10000 + b"NEWEST exception\n")
    result = log_scan.search_source(
        LocalFiles(), {"path": str(path)}, ".*exception", regex=True, max_bytes=4096
    )
    assert len(result["matches"]) == 1
    assert result["matches"][0]["line"] is None
    assert result["coverage_end"] == path.stat().st_size
    assert result["uncovered"] == [{"start": 0, "end": path.stat().st_size - 4096}]
    assert not result["complete"]


def test_tail_window_start_is_not_a_real_regex_line_boundary(tmp_path):
    path = tmp_path / "fragment.log"
    path.write_bytes(b"prefixNEEDLE\n")
    result = log_scan.search_source(
        LocalFiles(), {"path": str(path)}, "^NEEDLE", regex=True, max_bytes=7
    )
    assert result["matches"] == [] and result["long_regex_lines"] == 1
    literal = log_scan.search_source(
        LocalFiles(), {"path": str(path)}, "NEEDLE", max_bytes=7
    )
    assert literal["matches"][0]["fragment"]


@pytest.mark.parametrize(
    "raw,secret",
    [
        ("HF_TOKEN=hf_hidden123", "hf_hidden123"),
        ('{"WANDB_API_KEY": "abc123private"}', "abc123private"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("https://secretvalue@github.com/a/b", "secretvalue"),
        ("--password actual-password", "actual-password"),
    ],
)
def test_secret_redaction_common_research_credentials(raw, secret):
    assert secret not in redact(raw)
    assert "[REDACTED]" in redact(raw)


def test_sigwinch_wakes_waiter_and_restores_signal_state():
    before = signal.getsignal(signal.SIGWINCH)
    read, write = os.pipe()
    wake = UIWakeup()
    waiter = UIWaiter(read, wake)
    try:
        assert waiter.bind_resize()
        trigger = threading.Timer(0.04, lambda: os.kill(os.getpid(), signal.SIGWINCH))
        trigger.start()
        started = time.monotonic()
        assert "publication" in waiter.wait(2)
        trigger.join()
        assert time.monotonic() - started < 0.5 and waiter.resized
    finally:
        waiter.close()
        wake.close()
        os.close(read)
        os.close(write)
    assert signal.getsignal(signal.SIGWINCH) == before


def test_notification_burst_is_bounded_and_source_work_still_runs():
    scheduler = WorkerScheduler(workers=1)
    gate, running = threading.Event(), threading.Event()
    lane = scheduler.lane("notification")
    source = scheduler.lane("source")

    def slow():
        running.set()
        gate.wait(3)

    try:
        lane.submit(slow)
        assert running.wait(1)
        futures = [lane.submit(lambda: None) for _ in range(600)]
        status = scheduler.status()
        assert status["lanes"]["notification"]["queued"] == 32
        assert status["notification_dropped"] == 568
        assert sum(future.cancelled() for future in futures) == 568
        assert source.submit(lambda: "fresh queue").result(timeout=1) == "fresh queue"
    finally:
        gate.set()
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_hook_timeout_stops_descendants_even_when_shell_exits_first(tmp_path):
    pidfile = tmp_path / "descendant.pid"
    program = (
        "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open(%r,'w').write(str(os.getpid())); time.sleep(30)"
        % str(pidfile)
    )
    import shlex

    command = shlex.join([sys.executable, "-c", program]) + " & wait"
    with pytest.raises(subprocess.TimeoutExpired):
        run_hook(command, [{"kind": "start", "job": "73"}], timeout=0.15)
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        except FileNotFoundError:
            break
        if state == "Z":
            break  # Killed descendant awaits its system reaper, not running.
        time.sleep(0.01)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail("Timed-out hook left a running descendant")


def test_historical_capture_serializes_outside_publication_lock(monkeypatch):
    from raven import model

    store = Store(persist=False)
    store.jobs = [Job("73", "fixture", "main", "RUNNING")]
    original = model.to_plain

    def checked(value):
        assert not store.lock._is_owned()
        return original(value)

    monkeypatch.setattr(model, "to_plain", checked)
    value = historical_replay.validate(historical_replay.capture(store, "73"))
    assert value["jobs"][0]["id"] == "73"


def test_queued_clipboard_is_tracked_and_runs_off_ui_thread(tmp_path, monkeypatch):
    scheduler = WorkerScheduler(workers=1)
    app = SimpleNamespace(
        interactive=True, sampler=SimpleNamespace(worker_scheduler=scheduler)
    )
    app.say = lambda value: setattr(app, "message", value)
    event = threading.Event()
    called = []

    def copy(text, state_dir, **options):
        called.append((text, threading.get_ident()))
        event.wait(1)
        return "copied exact selection"

    monkeypatch.setattr(clipboard, "copy", copy)
    try:
        owner = threading.get_ident()
        assert "queued" in clipboard.copy_for_app(app, "first immutable", str(tmp_path))
        assert len(app.clipboard_pending) == 1
        assert "queued" in clipboard.copy_for_app(
            app, "second immutable", str(tmp_path)
        )
        assert len(app.clipboard_pending) == 2
        event.set()
        for future in app.clipboard_pending:
            future.result(timeout=2)
        clipboard.poll(app)
        assert app.clipboard_pending == []
        assert [text for text, _ in called] == ["first immutable", "second immutable"]
        assert all(thread != owner for _, thread in called)
    finally:
        event.set()
        scheduler.shutdown(wait=True)


def test_default_quiet_hours_follow_the_local_timezone():
    from raven.alerts import AlertEngine

    engine = AlertEngine([], Store(persist=False))
    assert engine.quiet_zone == "local"
    engine.set_quiet("22:00", "07:00")
    assert engine.quiet_zone == "local"
    engine.close()


@pytest.mark.parametrize(
    "rows,cols,history,byte_limit",
    [(1, 20, 0, 0), (2, 20, 3, 32), (24, 80, 2000, 8 << 20), (8, 32, 100, 256)],
)
def test_bulk_terminal_scrollback_matches_full_vt_parser(
    rows, cols, history, byte_limit
):
    from raven.terminal_emulator import TerminalEngine

    fast = TerminalEngine(rows, cols, history_limit=history, history_bytes=byte_limit)
    slow = TerminalEngine(rows, cols, history_limit=history, history_bytes=byte_limit)
    slow._bulk_plain_lines = lambda text: 0
    for payload in (
        b"\x1b[32m",
        b"prior\r\n" * 3,
        b"hello  \r\n" * (history + rows + 3000),
        b"tail\x00AFTER",
    ):
        fast.feed(payload)
        slow.feed(payload)
    assert fast.snapshot() == slow.snapshot()
    assert "AFTER" in str(fast.snapshot()["lines"])


@pytest.mark.parametrize(
    "tab",
    [
        "jobs",
        "history",
        "group",
        "deps",
        "cluster",
        "nodes",
        "sources",
        "log",
        "analytics",
        "research",
    ],
)
def test_all_native_workspace_pages_have_publication_idle_tokens(tab):
    from raven.config import Config
    from raven.controller import App
    from raven.screen import _quiet_jobs_token
    from raven.startup import restore

    app = App(
        Store(persist=False),
        None,
        None,
        Config({"animations": False, "raven": {"managed": True}}),
        "test",
        interactive=True,
    )
    restore(app, {"enabled": False})
    app.tab = tab
    assert _quiet_jobs_token(app) is not None
    if app.research:
        app.research.close()


def test_raster_strips_complete_terminal_payload_before_cell_measurement():
    from raven.screen import _DifferentialPainter

    raw = "safe\x1b]52;c;ZXZpbA==\x07tail\u202e!"
    raster = _DifferentialPainter._raster([(0, [(raw, "")])], 60)
    text = "".join(cell[0] for cell in raster).rstrip()
    assert text == display_text(raw)
    assert not any(c in text for c in "\x1b\x07\u202e")


def test_alert_publication_batches_one_command_and_every_event(monkeypatch):
    from raven import alerts

    store = Store(persist=False)
    store.jobs = [Job(str(i), "running", "main", "RUNNING") for i in range(600)]
    invocations = []
    monkeypatch.setattr(
        alerts,
        "run_hook",
        lambda command, events, **kwargs: invocations.append((command, events)),
    )
    engine = alerts.AlertEngine(
        [
            dict(
                name="running",
                when="running",
                every=0,
                actions=["command"],
                command="authorized-hook",
            )
        ],
        store,
    )
    try:
        fired = engine.check(store.render_snapshot())
        engine.pool.shutdown(wait=True)
        assert len(fired) == 600 and len(invocations) == 1
        assert len(invocations[0][1]) == 600
        assert {event["job"] for event in invocations[0][1]} == {
            str(i) for i in range(600)
        }
    finally:
        engine.close()


def test_terminal_compatibility_aliases_preserve_module_identity():
    import importlib

    for old, new in (
        ("terminal_emulator", "emulator"),
        ("terminal_service", "service"),
        ("terminal_broker", "broker"),
        ("terminal_recording", "recording"),
    ):
        assert importlib.import_module("raven." + old) is importlib.import_module(
            "raven.terminal." + new
        )


def test_terminal_runtime_does_not_import_ui_controller_or_views():
    import ast
    import raven.terminal

    base = Path(raven.terminal.__file__).parent
    forbidden = {
        "controller",
        "views",
        "screen",
        "terminal_ui",
        "workbench",
        "job_panels",
        "curses",
    }
    for path in base.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""] + [item.name for item in node.names]
            else:
                continue
            assert not any(set(name.split(".")) & forbidden for name in imports), (
                path,
                node.lineno,
                imports,
            )


def test_clipboard_uses_injected_application_service_without_pane_imports(monkeypatch):
    scheduler = WorkerScheduler(workers=1)
    app = SimpleNamespace(
        interactive=True,
        sampler=None,
        research=None,
        background_worker_scheduler=lambda: scheduler,
        say=lambda value: None,
    )
    monkeypatch.setattr(clipboard, "copy", lambda *args, **kwargs: "done")
    try:
        assert "queued" in clipboard.copy_for_app(app, "captured")
        app.clipboard_pending[0].result(timeout=2)
        clipboard.poll(app)
        assert not app.clipboard_pending
        import ast

        tree = ast.parse(Path(clipboard.__file__).read_text())
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module == "job_panels"
            for node in ast.walk(tree)
        )
    finally:
        scheduler.shutdown(wait=True)


def test_even_small_interactive_retained_indexes_run_on_the_worker(tmp_path):
    from raven import log_tools
    from raven.logs import LogSession
    from raven.research import ResearchHub

    path = tmp_path / "small.log"
    path.write_text("ERROR one\nplain\n")
    logs = LogSession()
    logs.search = "ERROR"
    hub = ResearchHub({}, LocalFiles())
    app = SimpleNamespace(
        logs=logs,
        interactive=True,
        research=hub,
        cfg={},
        mode="main",
        tab="log",
        say=lambda value: None,
        fail=lambda value: pytest.fail(value),
    )
    try:
        buffer = logs.buffer(str(path))
        assert log_tools.count_retained(app, buffer) == 0
        assert app.log_tools_state["retained_pending"]
        hub.future.result(timeout=2)
        hub.poll_task()
        assert log_tools.count_retained(app, buffer) == 1
    finally:
        hub.close()


def test_clock_patch_keeps_source_age_and_pending_wait_fresh(monkeypatch):
    from raven import clock
    from raven.screen import _FrameCache

    cache = _FrameCache()
    cache.rows = [[("old", "")], [("old", "")]]
    cache.wait_slots = ((0, 0, 940.0, 20),)
    cache.source_clock_slots = ((1, 0, 998.0, 12),)
    monkeypatch.setattr(clock, "now", lambda: 1001.0)
    cache._patch_clock(None)
    assert cache.rows[0][0][0].strip().startswith("waited 1m")
    assert cache.rows[1][0][0].strip() == "3s ago"
    assert cache.clock_changed
