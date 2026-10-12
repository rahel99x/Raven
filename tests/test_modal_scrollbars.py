"""Modal scrollbars preserve source identity, focus and complete viewport data."""
from types import SimpleNamespace

import pytest

from raven import (activity_ui, command_ui, execution_ui, history_log_export,
                   layout as L, modal_scrollbars as B, navigation_tools,
                   navigation_ui, project_ui, scrollbars as S, scrolling,
                   session_tools, table_tools, table_ui)
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Store
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"animations": False, "log_lines": 0})
    app = App(Store(persist=False), None, None, cfg, "tester", interactive=False)
    app.width, app.height = 96, 18
    views = Views(L.Glyphs(False), cfg)
    return app, views


def source(dashboard, kind, monkeypatch):
    app, views = dashboard
    rows = [[(f" item-{i:02} payload", "")] for i in range(60)]
    if kind == "commands":
        app.mode, app.palette_edit = "palette", "test"
        monkeypatch.setattr(command_ui, "suggestions", lambda app: [
            {"value": f"item-{i:02}", "description": "cached option", "kind": "command"} for i in range(60)])
        monkeypatch.setattr(command_ui, "validation", lambda app: (False, "cached validation"))
        module, key = command_ui, "modal:commands"
    elif kind == "help":
        app.mode = "help"
        monkeypatch.setattr(command_ui, "_help_rows", lambda *args: (rows, len(rows)))
        module, key = command_ui, "modal:help"
    elif kind == "confirmation":
        app.mode, app.confirm = "confirm", {"action": "cancel", "ids": ["1"]}
        monkeypatch.setattr(command_ui, "_confirm_rows", lambda *args: ("Review cached items", rows))
        module, key = command_ui, "modal:confirmation"
    elif kind == "workspaces":
        app.mode = "workspace_picker"
        monkeypatch.setattr(navigation_ui, "_workspaces", lambda app: [(str(i), f"item-{i:02}", "cached workspace") for i in range(60)])
        module, key = navigation_ui, "modal:workspaces"
    elif kind == "navigation-list":
        app.mode = "locations_picker"
        values = [(f"item-{i:02}", "cached location") for i in range(60)]
        return app, views, "modal:navigation-list", lambda: navigation_tools._list_overlay(
            views, app, app.width, app.height, "Locations", values, "Choose cached location", [])
    elif kind == "full-value":
        app.mode = "value_peek"
        navigation_tools.initialize(app).update(value="\n".join(f"item-{i:02}" for i in range(60)), field="Long value")
        module, key = navigation_tools, "modal:full-value"
    elif kind == "table-tools":
        app.mode = "table_tools"
        app.table_tools_state.update(modal="marks", tab="history")
        items = [(str(i), Finished(str(i), f"item-{i:02}", state="COMPLETED"), False) for i in range(60)]
        monkeypatch.setattr(table_tools, "_selection", lambda *args: (items, items[0]))
        module, key = table_tools, "modal:table-tools"
    elif kind == "columns":
        app.mode = "columns"
        app.table_state["tab"] = "jobs"
        monkeypatch.setattr(table_ui, "ordered_definitions", lambda *args: [
            SimpleNamespace(key=f"field{i}", title=f"item-{i:02}") for i in range(60)])
        module, key = table_ui, "modal:columns"
    elif kind == "exports":
        app.mode = "exports"
        app.activity.exports = [{"id": str(i), "label": f"item-{i:02}", "path": f"/cached/{i}"} for i in range(60)]
        module, key = activity_ui, "modal:exports"
    elif kind == "export-preview":
        app.mode = "export_preview"
        app.activity.export_preview = {"path": "/cached/export", "label": "Preview", "scroll": 0,
                                       "lines": [f"item-{i:02}" for i in range(60)]}
        module, key = activity_ui, "modal:export-preview"
    elif kind == "activity":
        app.mode = "activity"
        for i in reversed(range(60)):
            app.activity.post(f"item-{i:02}")
        module, key = activity_ui, "modal:activity"
    elif kind == "inbox":
        app.mode = "session_inbox"
        items = [{"key": str(i), "unread": True, "failed": False,
                  "record": Finished(str(i), f"item-{i:02}", state="COMPLETED")} for i in range(60)]
        monkeypatch.setattr(session_tools, "observe", lambda *args: None)
        monkeypatch.setattr(session_tools, "inbox_items", lambda app: items)
        module, key = session_tools, "modal:inbox"
    elif kind == "alerts":
        app.mode = "session_alerts"
        app.store.alerts = SimpleNamespace(rules=[SimpleNamespace(name=f"item-{i:02}", when="cpu > 90", active=set()) for i in range(60)],
                                          controls_snapshot=lambda: {"quiet": None, "zone": "UTC", "snoozes": []},
                                          notification_muted=lambda name: False)
        module, key = session_tools, "modal:alerts"
    elif kind == "diagnostics":
        app.mode = "terminal_diagnostics"
        session_tools.initialize(app)["diagnostics"] = [{"name": f"item-{i:02}", "status": "ok", "detail": "cached evidence"} for i in range(60)]
        module, key = session_tools, "modal:diagnostics"
    elif kind in ("project-runs", "project-outputs", "project-preview"):
        state = project_ui.initialize(app)
        state["root"] = "/cached/project"
        if kind == "project-runs":
            app.mode = "project_runs"
            state["runs"] = [{"run_id": f"item-{i:02}", "attempt": 1, "state": "COMPLETED", "job_id": str(i)} for i in range(60)]
        elif kind == "project-outputs":
            app.mode = "project_outputs"
            state["tree"] = {"status": "valid", "nodes": [{"path": f"item-{i:02}", "name": f"item-{i:02}", "directory": False,
                                                            "depth": 0, "status": "valid"} for i in range(60)]}
        else:
            app.mode = "project_preview"
            state["preview"] = {"path": "/cached/artifact", "format": "text", "status": "ready", "lines": [f"item-{i:02}" for i in range(60)]}
        module, key = project_ui, "modal:" + kind
    elif kind == "execution-details":
        app.mode = "execution"
        execution_ui.initialize(app).update(detail=True, plan={"command": "true", "valid": True,
            "issues": [{"level": "warning", "message": f"item-{i:02}"} for i in range(60)]})
        module, key = execution_ui, "modal:execution-details"
    elif kind == "execution-nodes":
        app.mode = "execution"
        execution_ui.initialize(app).update(view="review", review={"nodes": [
            {"id": f"item-{i:02}", "depends_on": [], "plan": {}} for i in range(60)]})
        module, key = execution_ui, "modal:execution-nodes"
    elif kind == "history-log-actions":
        app.mode = "history_log_picker"
        history_log_export.initialize(app).update(stage="picker", cwd="/cached/project", jobs=("1",))
        monkeypatch.setattr(history_log_export, "_items", lambda app: [(f"item-{i:02}", ("directory", str(i))) for i in range(60)])
        module, key = history_log_export, "modal:history-log-actions"
    elif kind == "history-log-report":
        app.mode = "history_log_missing"
        history_log_export.initialize(app).update(stage="missing", jobs=("1",))
        monkeypatch.setattr(history_log_export, "_detail_count", lambda app: 60)
        monkeypatch.setattr(history_log_export, "_details", lambda app, start, count: [
            f"item-{i:02}" for i in range(start, min(60, start + count))])
        module, key = history_log_export, "modal:history-log-report"
    else:
        raise AssertionError(kind)
    return app, views, key, lambda: module.overlay(views, {}, app, app.width, app.height)


KINDS = ("commands", "help", "confirmation", "workspaces", "navigation-list", "full-value", "table-tools", "columns",
         "exports", "export-preview", "activity", "inbox", "alerts", "diagnostics", "project-runs",
         "project-outputs", "project-preview", "execution-details", "execution-nodes", "history-log-actions", "history-log-report")


def draw(app, render, key):
    S.begin_frame(app)
    scrolling.begin_frame(app)
    rows = render()
    scrolling.finish_frame(app)
    panes = S.publish(app, app.width, app.height, overlays=rows)
    pane = next(pane for pane in panes if pane.key == key)
    return rows, pane


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("ascii_", (False, True))
def test_modal_bottom_and_top_are_complete_and_preserve_selection(dashboard, monkeypatch, kind, ascii_):
    app, views, key, render = source(dashboard, kind, monkeypatch)
    views.g = L.Glyphs(ascii_)
    before_cursor = dict(app.cursor)
    before_marks, before_selected = set(app.marks), app.selected_id
    rows, pane = draw(app, render, key)
    assert pane.absolute and pane.layer == 1 and pane.limit > 0
    assert pane.header is not None
    assert pane.rect.top < pane.rect.bottom <= app.height
    assert 0 <= pane.rect.left < pane.rect.right <= app.width
    assert all(L.vlen(L.row_text(row)) <= app.width - x for _, x, row in rows)
    y, left, _ = pane.header
    monkeypatch.setattr(app.store, "snapshot", lambda: pytest.fail("Scrollbar pointer input must not read the store"))
    assert S.handle_mouse(app, y, left + 2, button="left")
    rows, pane = draw(app, render, key)
    assert pane.target == pane.painted == pane.limit
    assert "item-59" in "\n".join(L.row_text(row) for _, _, row in rows)
    assert app.cursor == before_cursor and app.marks == before_marks and app.selected_id == before_selected
    assert S.handle_mouse(app, pane.header[0], pane.header[1], button="left")
    rows, pane = draw(app, render, key)
    assert pane.target == pane.painted == 0
    assert "item-00" in "\n".join(L.row_text(row) for _, _, row in rows) or kind == "execution-details"


@pytest.mark.parametrize("kind", KINDS)
def test_modal_source_change_discards_old_manual_position(dashboard, monkeypatch, kind):
    app, _, key, render = source(dashboard, kind, monkeypatch)
    _, pane = draw(app, render, key)
    S.set_manual(app, key, pane.limit, context=pane.context)
    assert S.manual(app, key, context=pane.context) == pane.limit
    assert S.manual(app, key, context=("different source",)) is None
    _, new = draw(app, render, key)
    assert new.target == 0


@pytest.mark.parametrize("width,height", [(96, 18), (40, 12), (16, 8), (7, 4), (3, 2), (0, 0)])
def test_box_adapter_clips_to_physical_geometry_without_source_work(width, height):
    app = SimpleNamespace(width=width, height=height, mode="help", tab="jobs", animations_enabled=False)
    rows = [[("header", "cyan")]] + [[("L" * max(1, width), "green+bg:surface")] for _ in range(5)]
    raw = L.box(L.Glyphs(False), rows, width, height, "Long source")
    S.begin_frame(app)
    output = B.boxed(app, "test", raw, start=1, count=60, page=5, target=0, painted=0,
                     setter=lambda value: None)
    assert len(output) == len(raw)
    assert all(0 <= x <= width and 0 <= y < height and L.vlen(L.row_text(row)) <= width - x for y, x, row in output)
    for pane in S.publish(app, width, height):
        assert 0 <= pane.rect.top < pane.rect.bottom <= height
        assert 0 <= pane.rect.left < pane.rect.right <= width


def test_cursor_change_resumes_autoreveal_without_selecting_during_scroll():
    app = SimpleNamespace(animations_enabled=False, mode="main", tab="jobs")
    assert B.window(app, "list", 0, 100, 8, context=("file",), focus="first") == (0, 0)
    S.set_manual(app, "list", 80, context=("file",))
    assert B.window(app, "list", 0, 100, 8, context=("file",), focus="first") == (80, 80)
    assert B.window(app, "list", 1, 100, 8, context=("file",), focus="second") == (1, 1)


@pytest.mark.parametrize("kind", ("alerts", "diagnostics"))
@pytest.mark.parametrize("ascii_", (False, True))
def test_multirow_modal_cross_page_selection_copies_exact_physical_lines(dashboard, monkeypatch, kind, ascii_):
    from raven import clipboard, text_selection as T
    app, views, key, render = source(dashboard, kind, monkeypatch)
    views.g = L.Glyphs(ascii_)
    if kind == "diagnostics":
        for index, item in enumerate(app.session_tools_state["diagnostics"]):
            item["detail"] = ("", "short evidence", "A" * 180)[index % 3]
    rows, pane = draw(app, render, key)
    T.publish(app, [], app.width, app.height, overlays=rows)
    state = T.initialize(app)
    expected = dict(state["visible"][key])
    assert T.handle_mouse(app, pane.rect.top, pane.rect.left + 1, button="left")
    assert T.initialize(app)["selection"]["key"] == key
    monkeypatch.setattr(app.store, "snapshot", lambda: pytest.fail("Text scrolling must use published data"))
    # Odd offsets start inside a card. Successive overlapping windows must
    # retain the same logical indices for every heading and detail line.
    for offset in (1, pane.page - 1, 2 * pane.page - 2):
        S.set_manual(app, key, offset, context=pane.context)
        rows, pane = draw(app, render, key)
        T.publish(app, [], app.width, app.height, overlays=rows)
        visible = dict(T.initialize(app)["visible"][key])
        for index, text in visible.items():
            if index in expected:
                assert expected[index] == text
            expected[index] = text
        assert T.handle_mouse(app, pane.rect.bottom - 1, pane.rect.left + 1, button="left", shift=True)
    selection = T.initialize(app)["selection"]
    end = selection["end"]
    assert set(range(end + 1)) <= expected.keys()
    exact = "\n".join(expected[index] for index in range(end + 1)) + "\n"
    assert T.selection_text(app) == exact
    copied = []
    monkeypatch.setattr(clipboard, "copy", lambda text, *args, **kwargs: copied.append(text) or "copied")
    assert T.copy_selection(app)
    assert copied == [exact]


def test_diagnostic_row_index_is_cached_between_scroll_frames(dashboard, monkeypatch):
    app, _, key, render = source(dashboard, "diagnostics", monkeypatch)
    _, pane = draw(app, render, key)
    index = app.session_tools_state["diagnostic_row_index"]
    offsets = index["offsets"]
    # The renderer cleans only the handful of visible cards on later paints;
    # counting all diagnostic text again would violate the scroll work budget.
    original = session_tools.clean
    calls = []
    def observed(text, *args, **kwargs):
        calls.append(text)
        return original(text, *args, **kwargs)
    monkeypatch.setattr(session_tools, "clean", observed)
    S.set_manual(app, key, 1, context=pane.context)
    draw(app, render, key)
    assert app.session_tools_state["diagnostic_row_index"]["offsets"] is offsets
    assert len(calls) <= pane.page + 1
