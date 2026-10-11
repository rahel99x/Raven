"""Real application/PTY/bridge acceptance tests for shared terminal ownership."""
from __future__ import annotations

import curses
import os
from pathlib import Path
import shlex
import sys
import time

import pytest

from tower import layout, screen, terminal_ui as T, terminal_fusion as F
from tower.config import Config
from tower.controller import App
from tower.model import Finished, Job, Store
from tower.views import Views

ROOT = Path(__file__).resolve().parents[1]


def text(app, sid=None):
    state = app.terminal_state
    snapshot = state.service.snapshot(sid or state.active)
    return "\n".join("".join(segment[0] for segment in row) for row in snapshot.get("lines", ()))


def until(app, predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        T.tick(app)
        if predicate():
            return
        time.sleep(.005)
    pytest.fail("Terminal condition timed out: " + repr(app.terminal_state.service.sessions))


def key(app, value):
    screen._apply_input(app, screen.InputEvent((value, None), origin=T.input_identity(app)), app.last_hits, curses)


def command(app, value):
    screen._apply_input(app, screen.InputEvent(("paste", value), origin=T.input_identity(app)), app.last_hits, curses)
    key(app, "enter")


def click(app, y, x):
    for ending in ("M", "m"):
        event = screen._mouse_report(0, x + 1, y + 1, ending, curses)
        screen._apply_input(app, screen.InputEvent(("mouse", event), origin=T.input_identity(app),
                           geometry=screen._terminal_geometry(app)), app.last_hits, curses)


def control(app, args):
    match = next((row for row in app.terminal_state.controls if tuple(args) == row[3]), None)
    assert match is not None, (args, app.terminal_state.controls)
    y, left, right, _ = match
    click(app, y, left + max(0, right-left-1)//2)


@pytest.fixture
def tower(tmp_path):
    pytest.importorskip("pyte")
    cfg = Config({"animations": False, "startup_animation": False, "gpu_sampling": False})
    store = Store(persist=False)
    store.jobs = [Job("701", "first-project", "cpu", "RUNNING", cpus=4, user="test", start="2026-10-11T01:00:00"),
                  Job("702", "other-project", "cpu", "PENDING", cpus=8, user="test")]
    store.finished = [Finished("700", "previous-project", "COMPLETED", cpus=4, user="test")]
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(layout.Glyphs(False), cfg)
    app.views_ref = views
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, None, 160, 48)
    yield app, views, store, cache, tmp_path
    T.shutdown(app)


def shell(tower, label="integration"):
    app, views, store, cache, tmp_path = tower
    sid = T.open_session(app, argv=["/bin/bash", "--noprofile", "--norc", "-i"], cwd=str(tmp_path),
                         label=label, metadata={"shell": "/bin/bash", "host": __import__('socket').gethostname()},
                         env={"PS1": "FUSION_READY> ", "PYTHONPATH": str(ROOT)})
    assert sid, app.message
    cache.rebuild(app, views, store, None, 160, 48)
    until(app, lambda: "FUSION_READY> " in text(app))
    return sid


@pytest.mark.parametrize("page", ["jobs", "history", "analytics", "deps", "log", "research"])
def test_real_shell_shared_across_native_pages_and_details_switches(tower, page):
    app, views, store, cache, tmp_path = tower
    sid = shell(tower)
    command(app, "FUSION_KEEP=37")
    until(app, lambda: "FUSION_KEEP=37" in text(app))
    # Page changes are ordinary Tower navigation after explicit focus escape.
    key(app, "ctrl-]"); key(app, "t")
    assert not T.focused(app)
    app.tab = page
    cache.rebuild(app, views, store, None, 160, 48)
    control(app, ["terminal", "show"])
    cache.rebuild(app, views, store, None, 160, 48)
    assert T.focused(app) and app.terminal_state.active == sid
    assert len(app.terminal_state.service.sessions) == 1
    command(app, "printf 'KEPT:%s\\n' \"$FUSION_KEEP\"")
    until(app, lambda: "KEPT:37" in text(app))
    rows, _, _ = cache.feedback(app, views)
    assert "KEPT:37" in "\n".join(layout.row_text(row) for row in rows)
    control(app, ["terminal", "details"])
    cache.rebuild(app, views, store, None, 160, 48)
    assert not T.showing(app) and not T.focused(app)
    assert app.terminal_state.service.snapshot(sid)["status"] == "running"


def test_real_shell_menu_can_reach_last_action_at_narrow_size(tower):
    app, views, store, cache, _ = tower
    shell(tower)
    app.terminal_state.menu = False
    T.run_command(app, ["terminal", "menu"])
    cache.rebuild(app, views, store, None, 78, 19)
    items = T._actions()
    for _ in range(len(items) - 1):
        key(app, "down")
    rows, _, _ = cache.feedback(app, views)
    state = app.terminal_state
    assert state.menu_index == len(items)-1
    assert any(row[3] == tuple(items[-1][1]) for row in state.controls)
    assert all(layout.vlen(layout.row_text(row)) <= 78 for row in rows)
    control(app, items[-1][1])
    cache.feedback(app, views)
    assert state.report is not None or state.dialog is not None or state.preview is not None
    assert app.tab == "jobs"


def test_real_bridge_cli_connects_output_submission_and_research_context(tower):
    app, views, store, cache, tmp_path = tower
    sid = shell(tower)
    py = shlex.quote(sys.executable)
    report = py + " -m tower terminal report "
    # The child receives a real private socket capability from open_session.
    command(app, report + "prompt --cwd " + shlex.quote(str(tmp_path)))
    until(app, lambda: app.terminal_fusion_state["store"].sessions[sid]["state"] == "prompt")
    command(app, report + "submission --job 701")
    until(app, lambda: app.terminal_fusion_state["store"].sessions[sid]["jobs"])
    command(app, report + "project --project project-a")
    until(app, lambda: app.terminal_fusion_state["store"].sessions[sid]["project"] == str(tmp_path / "project-a"))
    command(app, report + "start --command integration-command")
    until(app, lambda: bool(app.terminal_fusion_state["store"].blocks))
    command(app, "printf 'BRIDGE_RESULT:%s\\n' success")
    until(app, lambda: "BRIDGE_RESULT:success" in text(app))
    command(app, report + "end --status 7")
    until(app, lambda: any(value["status"] == 7 for value in app.terminal_fusion_state["store"].blocks.values()))
    block = next(iter(app.terminal_fusion_state["store"].blocks.values()))
    assert block["jobs"][0]["id"] == "701"
    assert block["project"] == str(tmp_path / "project-a")
    assert "BRIDGE_RESULT:success" in block["output"]
    T.run_command(app, ["terminal", "failures"])
    cache.feedback(app, views)
    assert block["id"] in repr(app.terminal_state.report)
    assert "TOWER_BRIDGE_TOKEN" not in repr(block)


def test_stale_shell_input_does_not_reach_new_real_session(tower):
    app, views, store, cache, _ = tower
    first = shell(tower, "first")
    pending = screen.InputEvent(("paste", "UNEXPECTED_CROSS_SESSION=1"), origin=T.input_identity(app))
    second = shell(tower, "second")
    assert first != second
    screen._apply_input(app, pending, app.last_hits, curses)
    command(app, "printf 'STATE:%s\\n' \"${UNEXPECTED_CROSS_SESSION-unset}\"")
    until(app, lambda: "STATE:unset" in text(app))
    T.run_command(app, ["terminal", "session", first])
    command(app, "printf 'FIRST:%s\\n' \"${UNEXPECTED_CROSS_SESSION-unset}\"")
    until(app, lambda: "FIRST:unset" in text(app))
    assert app.tab == "jobs" and not app.quit


def test_preview_and_dialog_input_cannot_execute_in_real_shell(tower):
    app, views, store, cache, tmp_path = tower
    shell(tower)
    command(app, "stty -echo")
    T.run_command(app, ["terminal", "rename"])
    cache.feedback(app, views)
    for value in "touch marker-file":
        key(app, "space" if value == " " else value)
    key(app, "enter")
    until(app, lambda: app.terminal_state.service.snapshot(app.terminal_state.active)["label"] == "touch marker-file")
    assert not (tmp_path / "marker-file").exists()
    T.run_command(app, ["terminal", "recipe", "save", "safe-cancel", "touch marker-file"])
    T.run_command(app, ["terminal", "recipe", "use", "safe-cancel"])
    cache.feedback(app, views)
    assert app.terminal_state.preview
    key(app, "esc")
    command(app, "printf 'PROMPT_ALIVE:%s\\n' yes")
    until(app, lambda: "PROMPT_ALIVE:yes" in text(app))
    assert not (tmp_path / "marker-file").exists()


@pytest.mark.parametrize("width,height", [(1, 1), (20, 6), (80, 10)])
def test_invisible_terminal_loses_keyboard_lease_and_rejects_queued_input(tower, width, height):
    app, views, store, cache, _ = tower
    sid = shell(tower)
    pending = screen.InputEvent(("paste", "INVISIBLE_DESTINATION=1"), origin=T.input_identity(app))
    cache.rebuild(app, views, store, None, width, height)
    assert not T.focused(app), "A shell must not retain keyboard ownership without visible usable content"
    screen._apply_input(app, pending, cache.hits, curses)
    cache.rebuild(app, views, store, None, 160, 48)
    control(app, ["terminal", "show"])
    command(app, "printf 'VISIBLE:%s\\n' \"${INVISIBLE_DESTINATION-unset}\"")
    until(app, lambda: "VISIBLE:unset" in text(app, sid))


def test_terminal_report_bottom_reaches_the_final_evidence_line(tower):
    app, views, store, cache, _ = tower
    shell(tower)
    T.show_report(app, [f"evidence-{number}" for number in range(100)], "Acceptance report")
    cache.rebuild(app, views, store, None, 100, 24)
    key(app, "end")
    rows, _, _ = cache.feedback(app, views)
    assert "evidence-99" in "\n".join(layout.row_text(row) for row in rows)


def test_real_bridge_completion_in_hidden_active_session_is_reported(tower):
    app, views, store, cache, _ = tower
    sid = shell(tower)
    report = shlex.quote(sys.executable) + " -m tower terminal report "
    command(app, report + "start --command hidden-command; sleep .2; " + report + "end --status 3")
    until(app, lambda: bool(app.terminal_fusion_state["store"].blocks))
    control(app, ["terminal", "details"])
    cache.rebuild(app, views, store, None, 160, 48)
    until(app, lambda: any(block["status"] == 3 for block in app.terminal_fusion_state["store"].blocks.values()))
    notices = app.terminal_fusion_state["store"].notices
    assert any(notice["session"] == sid and notice["status"] == 3 for notice in notices)
    assert "hidden terminal" in app.message


def test_real_terminal_does_not_steal_job_selection_drag_crossing_panes(tower):
    from tower import pointer_routing
    app, views, store, cache, _ = tower
    shell(tower)
    key(app, "ctrl-]"); key(app, "t")
    cache.rebuild(app, views, store, None, 160, 48)
    job = app.interaction_state["graph"].get("job:701")
    assert job is not None
    rect = app.terminal_state.body_rect
    frames = [(0, job.rect.left + 1, job.rect.top, "M"),
              (32, rect.x + 4, rect.y + 4, "M"), (0, rect.x + 4, rect.y + 4, "m")]
    for index, (code, x, y, ending) in enumerate(frames):
        report = screen._mouse_report(code, x + 1, y + 1, ending, curses)
        screen._apply_input(app, screen.InputEvent(("mouse", report), origin=T.input_identity(app),
                            geometry=screen._terminal_geometry(app)), cache.hits, curses)
        if index == 0:
            assert pointer_routing.active(app)
        assert not T.focused(app)
        assert app.terminal_state.capture is None
        assert app.tab == "jobs"
    assert not pointer_routing.active(app)
    assert app.terminal_state.service.snapshot(app.terminal_state.active)["status"] == "running"


def test_closing_real_session_revokes_bridge_capability_and_preserves_other_shell(tower):
    from tower.terminal_bridge import report
    app, views, store, cache, tmp_path = tower
    first = shell(tower, "first")
    env = app.terminal_fusion_state["bridge"].register(first)
    second = shell(tower, "second")
    T.run_command(app, ["terminal", "session", first])
    T.run_command(app, ["terminal", "close"])
    assert app.terminal_state.active == second
    report("prompt", env=env, cwd=str(tmp_path))
    until(app, lambda: app.terminal_fusion_state["bridge"].rejected > 0)
    assert first not in app.terminal_fusion_state["store"].sessions
    command(app, "printf 'REMAINING:%s\\n' alive")
    until(app, lambda: "REMAINING:alive" in text(app, second))
