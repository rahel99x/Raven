"""Operations input, review completeness, and terminal lifecycle checks."""
from types import SimpleNamespace
import threading
import time

import pytest

from raven import operations, ops_ui, screen
from test_operations_sequences import operation_dashboard, _ui_action


def test_graph_keyboard_enters_field_and_arrows_edit_its_text(operation_dashboard):
    app = operation_dashboard.app
    app.run_command("ops pending-edit")
    app.ops_state["values"]["value"] = "abc"
    operation_dashboard.draw()
    field = next(control for control in app.interaction_state["graph"].controls
                 if control.id == "ops-item:ops field value")
    app.interaction_state.update(active=True, focused=field.id)
    app.handle("enter")
    assert app.ops_state["editing"] == "value"
    operation_dashboard.draw()
    app.handle("home")
    app.handle("X")
    app.handle("right")
    app.handle("delete")
    app.handle("enter")
    assert app.ops_state["editing"] is None
    assert app.ops_state["values"]["value"] == "Xac"


def test_input_escape_keeps_committed_value(operation_dashboard):
    app = operation_dashboard.app
    app.run_command("ops pending-edit")
    app.ops_state["values"]["value"] = "10"
    app.run_command("ops field value")
    app.handle("end")
    app.handle("0")
    app.handle("esc")
    assert app.ops_state["values"]["value"] == "10"
    assert app.mode == "operations"


@pytest.mark.parametrize("large", ["rows", "wrapped"])
def test_truncated_review_cannot_be_confirmed(operation_dashboard, monkeypatch, large):
    app = operation_dashboard.app
    app.run_command("ops pending-edit")
    payload = {"items": list(range(ops_ui.MAX_LINES + 20))} if large == "rows" else {"text": "x" * 300000}
    plan = operations.prepare_plan(ops_ui.context(app), "pending-edit", {"job_id": "81"}, payload)
    app.ops_state.update(view="report", result=operations.report("pending-edit", "Large action", plan=plan))
    app.run_command("ops review")
    operation_dashboard.draw(28, 12)
    assert app.ops_state["review_truncated"]
    assert not any(control.id == "ops:apply" for control in app.interaction_state["graph"].controls)
    assert app.ops_state["confirm_digest"] is None
    def forbidden(*args, **kwargs):
        raise AssertionError("An incomplete review was applied")
    monkeypatch.setattr(operations, "apply", forbidden)
    app.run_command("ops apply")
    assert app.ops_state["view"] == "review"


def test_long_but_complete_review_does_not_silently_cut_json_values(operation_dashboard):
    app = operation_dashboard.app
    app.run_command("ops pending-edit")
    text = "x" * 20000 + "END-OF-EXACT-CONTENT"
    plan = operations.prepare_plan(ops_ui.context(app), "pending-edit", {"job_id": "81"}, {"text": text})
    app.ops_state.update(view="report", result=operations.report("pending-edit", "Long action", plan=plan))
    app.run_command("ops review")
    operation_dashboard.draw()
    assert not app.ops_state["review_truncated"]
    rendered = "".join(line for _, line, _ in app.ops_state["wrapped"][1])
    assert text in rendered
    assert app.ops_state["confirm_digest"] == plan["digest"]


def test_invalid_background_result_is_reported_without_navigation(operation_dashboard, monkeypatch):
    app = operation_dashboard.app
    monkeypatch.setattr(operations, "run", lambda *args: {"unexpected": True})
    app.run_command("ops run slurm-doctor")
    assert app.ops_state["result"]["status"] == "error"
    assert app.mode == "operations" and app.tab == "jobs"


@pytest.mark.parametrize("outcome", [0, 7, OSError("missing shell"), KeyboardInterrupt()])
def test_terminal_handoff_restores_input_after_all_child_outcomes(operation_dashboard, monkeypatch, outcome):
    app = operation_dashboard.app
    app.mode = "operations"
    app.ops_state["terminal"] = {"argv": ["srun", "--jobid=81", "/bin/bash"],
        "at": time.monotonic(), "scope": ops_ui.context(app).scope}
    events = []
    from raven import terminal_ui
    identity = ("reattached-session", 7)
    monkeypatch.setattr(terminal_ui, "input_identity", lambda owner: identity)
    delivered = []
    monkeypatch.setattr(terminal_ui, "handle_key", lambda owner, key: delivered.append(key))
    terminal = SimpleNamespace(touchwin=lambda: events.append("touch"), refresh=lambda: events.append("refresh"), get_wch=lambda: "a")
    curses = SimpleNamespace(endwin=lambda: events.append("endwin"), flushinp=lambda: events.append("flush"), error=RuntimeError)
    screen._INPUT_READERS[id(terminal)] = object()
    monkeypatch.setattr(screen, "_mouse_reporting", lambda enabled: events.append(("mouse", enabled)))
    def call(argv):
        events.append(tuple(argv))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr(screen.subprocess, "call", call)
    assert screen._operation_terminal(app, terminal, curses, True)
    reader = screen._INPUT_READERS[id(terminal)]
    assert reader.window is terminal
    assert callable(reader.input_owner) and callable(reader.geometry_owner)
    assert events[0:2] == ["endwin", ("mouse", False)]
    assert events[-4:] == ["flush", ("mouse", True), "touch", "refresh"]
    event = reader.read(__import__("curses"))
    assert event.origin == identity
    screen._apply_input(app, event, [], curses)
    assert delivered == ["a"]
    assert not screen._operation_terminal(app, terminal, curses, True)


def test_shutdown_drains_owned_action_callback_and_suppresses_handoff(operation_dashboard, monkeypatch):
    app = operation_dashboard.app
    _ui_action(monkeypatch)
    app.run_command("ops run pending-edit job_id=81 field=TimeLimit value=10")
    app.run_command("ops review")
    operation_dashboard.draw()
    entered, release = threading.Event(), threading.Event()
    def apply(feature, plan, ctx):
        entered.set()
        assert release.wait(3)
        assert not ctx.cancel.is_set()
        return operations.report(feature, "Recorded before exit", data={"receipt": "saved", "terminal_argv": ["srun"]})
    monkeypatch.setattr(operations, "apply", apply)
    app.interactive = True
    app.run_command("ops apply")
    assert entered.wait(3)
    app.quit = True
    assert ops_ui.defer_quit(app)
    assert not app.quit and app.ops_state["quit_requested"]
    timer = threading.Timer(.05, release.set)
    timer.start()
    try:
        ops_ui.shutdown(app)
    finally:
        release.set()
        timer.join()
    assert app.research.pending is None
    assert app.ops_state["result"]["data"]["receipt"] == "saved"
    assert not app.ops_state["running"]
    assert ops_ui.take_terminal(app) is None
    ops_ui.tick(app)
    assert app.quit


def test_shutdown_cancels_inspection_and_collects_only_its_own_future(operation_dashboard, monkeypatch):
    app = operation_dashboard.app
    entered = threading.Event()
    def inspect(feature, params, ctx):
        entered.set()
        assert ctx.cancel.wait(3)
        return operations.report(feature, "Cancelled evidence")
    monkeypatch.setattr(operations, "run", inspect)
    app.interactive = True
    app.run_command("ops run slurm-doctor")
    assert entered.wait(3)
    ops_ui.shutdown(app)
    assert app.research.pending is None
    assert not app.ops_state["running"]
    assert app.ops_state["result"] is None


def test_bracketed_paste_inserts_in_field_without_changing_or_executing_it(operation_dashboard, monkeypatch):
    app = operation_dashboard.app
    app.run_command("ops staging")
    app.ops_state["values"]["manifest"] = "/before/after.json"
    app.run_command("ops field manifest")
    app.ops_state["edit_cursor"] = 8
    def forbidden(*args, **kwargs):
        raise AssertionError("Paste executed an operation")
    monkeypatch.setattr(operations, "run", forbidden)
    monkeypatch.setattr(operations, "apply", forbidden)
    inserted = "folder with spaces/界/:ops apply/"
    screen._apply_input(app, ("paste", inserted), app.last_hits, SimpleNamespace())
    assert app.ops_state["edit"] == "/before/" + inserted + "after.json"
    assert app.ops_state["edit_cursor"] == 8 + len(inserted)
    assert app.ops_state["values"]["manifest"] == "/before/after.json"
    assert app.ops_state["editing"] == "manifest"
    assert app.ops_state["feature"] == "staging" and app.mode == "operations" and not app.quit
    app.handle("enter")
    assert app.ops_state["values"]["manifest"] == "/before/" + inserted + "after.json"
    assert app.ops_state["view"] == "form" and not app.ops_state["running"]


@pytest.mark.parametrize("payload", ["ops apply\nquit", "quit\r", "a\tb", "\x1b[<0;1;1M", "\x03",
    "\x00", "\x7f", "\x9b", "\udcff", "x" * (ops_ui.MAX_PASTE + 1)])
def test_hostile_or_oversized_field_paste_is_rejected_atomically(operation_dashboard, payload):
    app = operation_dashboard.app
    app.run_command("ops staging")
    app.ops_state["values"]["manifest"] = "original.json"
    app.run_command("ops field manifest")
    before = (app.ops_state["edit"], app.ops_state["edit_cursor"], app.ops_state["editing"], app.tab, app.mode)
    screen._apply_input(app, ("paste", payload), app.last_hits, SimpleNamespace())
    assert (app.ops_state["edit"], app.ops_state["edit_cursor"], app.ops_state["editing"], app.tab, app.mode) == before
    assert not app.quit
    assert "Paste" in app.message or "paste" in app.message


def test_field_paste_respects_remaining_capacity_without_truncating_path(operation_dashboard):
    app = operation_dashboard.app
    app.run_command("ops staging")
    app.run_command("ops field manifest")
    app.ops_state.update(edit="x" * (ops_ui.MAX_FIELD - 2), edit_cursor=ops_ui.MAX_FIELD - 2)
    screen._apply_input(app, ("paste", "abc"), app.last_hits, SimpleNamespace())
    assert len(app.ops_state["edit"]) == ops_ui.MAX_FIELD - 2
    screen._apply_input(app, ("paste", "ab"), app.last_hits, SimpleNamespace())
    assert app.ops_state["edit"].endswith("ab") and len(app.ops_state["edit"]) == ops_ui.MAX_FIELD


@pytest.mark.parametrize("view", ["catalog", "form", "review"])
def test_paste_cannot_open_tools_change_views_or_accept_a_review(operation_dashboard, monkeypatch, view):
    app = operation_dashboard.app
    _ui_action(monkeypatch)
    app.run_command("ops run pending-edit job_id=81 field=TimeLimit value=10")
    if view == "review":
        app.run_command("ops review")
    elif view == "form":
        app.run_command("ops form")
    else:
        app.run_command("ops")
    operation_dashboard.draw()
    before = app.mode, app.tab, app.ops_state["view"], app.ops_state["feature"]
    def forbidden(*args, **kwargs):
        raise AssertionError("Paste accepted a review or navigated")
    monkeypatch.setattr(app, "run_command", forbidden)
    monkeypatch.setattr(operations, "apply", forbidden)
    screen._apply_input(app, ("paste", ":ops apply"), app.last_hits, SimpleNamespace())
    assert (app.mode, app.tab, app.ops_state["view"], app.ops_state["feature"]) == before


def test_regular_command_paste_still_uses_the_command_palette(operation_dashboard):
    app = operation_dashboard.app
    assert app.mode == "main"
    screen._apply_input(app, ("paste", "ops staging"), app.last_hits, SimpleNamespace())
    assert app.mode == "palette"
    assert app.palette_edit == "ops staging"
    assert not app.ops_state["running"]


@pytest.mark.parametrize("ascii_mode", [False, True])
@pytest.mark.parametrize("width", [1, 6, 20, 60])
def test_field_caret_window_tracks_long_ascii_and_unicode_paths(ascii_mode, width):
    from raven import layout as L
    value = "/many/" * 200 + "界界a\u0301/end.json"
    for cursor in (0, 1, 600, len(value) - 1, len(value)):
        row = ops_ui._field_editor_row("Manifest pathname", value, cursor, width, ascii_mode)
        assert L.vlen(L.row_text(row)) <= width
        caret = [(text, style) for text, style in row if "rev" in style]
        assert len(caret) == 1 and 0 < L.vlen(caret[0][0]) <= width
        if ascii_mode:
            assert L.row_text(row).isascii()
    end = L.row_text(ops_ui._field_editor_row("Path", value, len(value), 60, ascii_mode))
    start = L.row_text(ops_ui._field_editor_row("Path", value, 0, 60, ascii_mode))
    assert "end.json" in end and "end.json" not in start


def test_raw_bracketed_paste_payload_stays_one_inert_field_edit(operation_dashboard):
    from test_raw_terminal_input import Window, events
    app = operation_dashboard.app
    app.run_command("ops staging")
    app.run_command("ops field manifest")
    value = "/project/界/ops apply.json"
    reader = screen._InputReader(Window(("\x1b[200~" + value + "\x1b[201~").encode()))
    decoded = events(reader)
    assert decoded == [("paste", value)]
    screen._apply_input(app, decoded[0], app.last_hits, SimpleNamespace())
    assert app.ops_state["edit"] == value
    assert app.ops_state["values"]["manifest"] == ""
    assert app.mode == "operations" and not app.quit
