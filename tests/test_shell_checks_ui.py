"""Shell-check review never runs disk IO in a frame or redirects late callbacks."""
from __future__ import annotations

from types import SimpleNamespace
import os
import sys
import time

import pytest

from raven import shell_checks, shell_checks_ui as UI, submission
from raven.layout import Glyphs, row_text, vlen


class Hub:
    def __init__(self):
        self.pending = None
        self.plan = None
        self.files = SimpleNamespace(remote=False)

    def start_task(self, function, callback):
        if self.pending:
            return False
        self.pending = function, callback
        return True

    def complete(self):
        function, callback = self.pending
        self.pending = None
        try:
            result = function()
        except Exception as exc:
            result = exc
        callback(result)

    def cancel_task(self, callback):
        if self.pending and self.pending[1] is callback:
            self.pending = None
            return True
        return False


@pytest.fixture
def app():
    messages = []
    return SimpleNamespace(mode="main", tab="jobs", interactive=True, replay=None, research=Hub(),
        files=SimpleNamespace(remote=False), cfg={}, width=100, height=32,
        say=lambda message: messages.append((True, message)), fail=lambda message: messages.append((False, message)), messages=messages)


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "batch.sh"
    path.write_text("#!/bin/bash\nprintf '%s\\n' hello\n")
    return path


def open_checked(app, script):
    assert UI.run_command(app, ["shellcheck", str(script)])
    assert app.mode == "shell_checks"
    app.research.complete()
    return UI.initialize(app)


def render(app, width=100, height=32):
    app.width, app.height = width, height
    return UI.overlay(SimpleNamespace(g=Glyphs(False)), {}, app, width, height)


def test_explicit_command_is_demand_driven_and_preserves_current_tab(app, script, monkeypatch):
    real = shell_checks.check_script
    calls = []
    monkeypatch.setattr(shell_checks, "check_script", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    assert UI.run_command(app, ["shellcheck", str(script)])
    assert not calls
    assert app.tab == "jobs"
    app.research.complete()
    assert len(calls) == 1 and UI.initialize(app)["result"]
    assert app.tab == "jobs"


def test_inactive_feature_never_consumes_keys_or_mouse(app):
    for mode in ("main", "help", "confirm", "execution"):
        app.mode = mode
        assert UI.handle_key(app, "enter") is False
        assert UI.handle_mouse(app, 0, 0) is False
        assert render(app) is None
    assert UI.run_command(app, ["different"]) is False


def test_prepared_plan_requires_exact_bytes_and_never_labels_stale_clean(app, script):
    plan = submission.prepare(str(script))
    app.research.plan = plan
    script.write_text(script.read_text() + "echo changed\n")
    UI.run_command(app, ["shellcheck"])
    app.research.complete()
    state = UI.initialize(app)
    assert state["result"]["status"] == "stale"
    assert "no matching" in UI.summary(app, plan)


def test_explicit_execution_plan_wins_and_returns_to_form(app, script):
    plan = submission.prepare(str(script))
    app.mode = "execution"
    app.execution_state = {"plan": plan}
    app.research.plan = {"script": "/wrong", "script_sha256": "wrong"}
    UI.run_command(app, ["shellcheck"])
    app.research.complete()
    assert UI.initialize(app)["expected"] == plan["script_sha256"]
    UI.handle_key(app, "esc")
    assert app.mode == "execution"


def test_missing_plan_and_invalid_arity_do_not_open_overlay(app):
    UI.run_command(app, ["shellcheck"])
    assert app.mode == "main" and "Prepare" in app.messages[-1][1]
    UI.run_command(app, ["shellcheck", "a", "b"])
    assert app.mode == "main" and "Quote" in app.messages[-1][1]


@pytest.mark.parametrize("where", ["app", "hub", "replay"])
def test_remote_and_replay_paths_are_never_read_as_local(app, script, where):
    if where == "app":
        app.files.remote = True
    elif where == "hub":
        app.research.files.remote = True
    else:
        app.replay = object()
    UI.run_command(app, ["shellcheck", str(script)])
    assert app.mode == "main" and app.research.pending is None
    assert not app.messages[-1][0]


def test_busy_worker_and_repeat_requests_are_bounded(app, script):
    app.research.pending = (lambda: None, lambda x: None)
    UI.run_command(app, ["shellcheck", str(script)])
    assert app.mode == "main" and not UI.initialize(app)["running"]
    app.research.pending = None
    UI.run_command(app, ["shellcheck", str(script)])
    pending = app.research.pending
    UI.run_command(app, ["shellcheck", str(script)])
    assert app.research.pending is pending


def test_escape_cancels_detaches_and_late_completion_cannot_reopen(app, script):
    UI.run_command(app, ["shellcheck", str(script)])
    old_work, old_callback = app.research.pending
    state = UI.initialize(app)
    event = state["cancel"]
    UI.handle_key(app, "esc")
    assert event.is_set() and app.research.pending is None
    app.tab = "history"
    old_callback({"status": "checked", "sha256": "old"})
    assert app.tab == "history" and app.mode == "main" and state["result"] is None


def test_old_result_never_overwrites_new_request(app, script):
    UI.run_command(app, ["shellcheck", str(script)])
    _, old_callback = app.research.pending
    UI.handle_key(app, "c")
    UI.handle_key(app, "r")
    old_callback({"status": "checked", "sha256": "old"})
    assert UI.initialize(app)["running"] and UI.initialize(app)["result"] is None
    app.research.complete()
    assert UI.initialize(app)["result"]["sha256"] != "old"


def test_repeated_rerun_key_never_cancels_a_new_request_before_repaint(app, script):
    state = open_checked(app, script)
    render(app)
    UI.handle_key(app, "r")
    event = state["cancel"]
    UI.handle_key(app, "r")
    UI.handle_key(app, "enter")
    assert state["running"] and not event.is_set()


def test_late_success_publishes_without_mode_or_tab_change(app, script):
    UI.run_command(app, ["shellcheck", str(script)])
    app.mode, app.tab = "main", "analytics"
    app.research.complete()
    assert app.mode == "main" and app.tab == "analytics"
    assert UI.initialize(app)["result"]


def test_refresh_reloads_changed_file_and_failure_is_displayable(app, script):
    state = open_checked(app, script)
    old = state["result"]["sha256"]
    script.write_text(script.read_text() + "false\n")
    UI.run_command(app, ["shellcheck", "refresh"])
    app.research.complete()
    assert state["result"]["sha256"] != old and not state["result"]["cached"]
    script.unlink()
    UI.handle_key(app, "r")
    app.research.complete()
    assert state["error"] and state["result"] is None
    assert "No such file" in " ".join(row_text(row) for _, _, row in render(app))


def test_renderer_is_cache_only_and_geometry_bounded_across_sizes(app, script, monkeypatch):
    open_checked(app, script)
    monkeypatch.setattr(shell_checks, "check_script", lambda *a, **k: pytest.fail("render invoked analyzer"))
    monkeypatch.setattr(shell_checks, "_capture", lambda *a, **k: pytest.fail("render read file"))
    for width, height in ((160, 50), (80, 24), (38, 12), (12, 6), (3, 2), (1, 1), (0, 0)):
        for _ in range(3):
            rows = render(app, width, height)
            for y, x, row in rows:
                assert 0 <= y < height and x >= 0 and x + vlen(row_text(row)) <= width
            state = UI.initialize(app)
            for y, _, hit in state["control_hits"]:
                assert 0 <= y < height and 0 <= hit["left"] < hit["right"] <= width


def test_keyboard_mouse_buttons_and_motion_never_click_through(app, script):
    state = open_checked(app, script)
    render(app)
    back = next(hit for hit in state["hits"] if hit[3] == "back")
    y, left, _, _ = back
    UI.handle_mouse(app, y, left, "motion")
    assert state["focus"] == "back" and app.mode == "shell_checks"
    assert UI.handle_mouse(app, -1, -1, "right")
    UI.handle_mouse(app, y, left, "release")
    assert app.mode == "shell_checks"  # unmatched release cannot activate
    UI.handle_mouse(app, y, left, "press")
    UI.handle_mouse(app, y, left, "release")
    assert app.mode == "main"
    open_checked(app, script)
    UI.handle_key(app, "tab")
    UI.handle_key(app, "enter")
    assert app.mode == "main"


def test_scroll_is_clamped_and_document_cache_reused(app, script):
    state = open_checked(app, script)
    render(app, 50, 14)
    document = state["document"]
    UI.handle_key(app, "end")
    assert state["scroll"] == max(0, state["count"] - state["page"])
    for _ in range(20):
        UI.handle_key(app, "down")
    assert state["scroll"] == max(0, state["count"] - state["page"])
    render(app, 50, 14)
    assert state["document"] is document
    UI.handle_key(app, "home")
    UI.handle_mouse(app, 3, 3, "wheel-up")
    assert state["scroll"] == 0


def test_stale_control_geometry_cannot_activate_after_rerun(app, script):
    state = open_checked(app, script)
    render(app)
    back = next(hit for hit in state["hits"] if hit[3] == "back")
    UI.handle_key(app, "r")
    UI.handle_mouse(app, back[0], back[1], "left")
    assert app.mode == "shell_checks" and state["running"]
    UI.close(app)
    assert not state["running"] and state["cancel"].is_set()


def test_noninteractive_command_publishes_machine_readable_evidence(app, script):
    app.interactive = False
    UI.run_command(app, ["shellcheck", str(script)])
    assert app.research_result["sha256"] and app.command_ok
    assert not UI.initialize(app)["running"] and app.research.pending is None


def test_execution_form_button_and_key_use_same_reviewed_plan(app, script):
    from raven import execution_ui
    from raven.workbench import bind_execution
    bind_execution(app)
    plan = submission.prepare(str(script))
    execution_ui.initialize(app)
    execution_ui._open_form(app, plan)
    execution_ui.overlay(SimpleNamespace(g=Glyphs(False)), {}, app, 130, 35)
    hit = next(hit for hit in app.execution_state["control_hits"] if hit[2]["id"] == "execution:shell-checks")
    execution_ui.handle_mouse(app, hit[0], hit[2]["left"])
    assert app.mode == "shell_checks"
    assert UI.initialize(app)["expected"] == plan["script_sha256"]
    UI.handle_key(app, "esc")
    execution_ui.handle_key(app, "h")
    assert app.mode == "shell_checks" and UI.initialize(app)["expected"] == plan["script_sha256"]


def test_semantic_graph_excludes_underlying_tabs_and_supports_hover_and_arrows(app, script):
    from raven import interaction
    from raven.workbench import bind_actions
    bind_actions(app)
    state = open_checked(app, script)
    overlays = render(app)
    app.selected_id = "1234"
    app.last_hits = [(1, "control", {"id": "hidden", "left": 1, "right": 8, "action": ("command", "tab logs")})]
    app.tab_hits = [(0, 0, 6, "research")]
    app.handle = lambda key: UI.handle_key(app, key)
    graph = interaction.publish(app, [[(" " * 100, "")]] * 32, app.last_hits, 100, 32, overlays)
    assert graph.get("shell-checks:rerun") and graph.get("shell-checks:back")
    assert graph.get("hidden") is None and graph.get("tab:research") is None
    control = graph.get("shell-checks:rerun")
    interaction.handle_mouse(app, control.rect.top, control.rect.left, button="motion")
    assert interaction.initialize(app)["hovered"] == "shell-checks:rerun"
    assert not state["running"] and app.tab == "jobs"
    interaction.handle_key(app, "f8")
    interaction.handle_key(app, "right")
    assert interaction.initialize(app)["focused"] == "shell-checks:back"
    interaction.handle_key(app, "enter")
    assert app.mode == "main" and app.tab == "jobs"


def test_real_worker_cancellation_reaps_live_analyzer_and_discards_publication(app, script, tmp_path, monkeypatch):
    from raven.research import ResearchHub
    marker = tmp_path / "analyzer-started"
    tool = tmp_path / "shellcheck"
    tool.write_text(f"#!{sys.executable}\nimport sys,time\n"
                    "if '--version' in sys.argv:\n print('test cancellation');sys.exit(0)\n"
                    f"open({str(marker)!r},'w').write('started')\ntime.sleep(30)\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", os.defpath))
    hub = app.research = ResearchHub({})
    try:
        UI.run_command(app, ["shellcheck", str(script)])
        future = hub.future
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists()
        UI.handle_key(app, "esc")
        result = future.result(timeout=2)
        assert result["status"] == "cancelled"
        hub.poll_task()
        assert app.mode == "main" and app.tab == "jobs" and not UI.initialize(app)["result"]
    finally:
        UI.close(app)
        hub.close()


def test_dense_findings_document_has_a_hard_render_bound(app, script, monkeypatch):
    state = open_checked(app, script)
    state["result"]["findings"] = [{"line": i, "column": 1, "code": "SC2086", "tool": "ShellCheck",
                                    "level": "warning", "message": "long finding " * 300} for i in range(512)]
    state["document"] = None
    monkeypatch.setattr(UI, "MAX_DOCUMENT_ROWS", 80)
    render(app, 40, 20)
    assert state["count"] == 80
    assert "More evidence" in row_text(state["document"][1][-1])
