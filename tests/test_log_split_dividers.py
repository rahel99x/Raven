"""Paired-source resize and docked log overlays preserve source navigation."""
from types import SimpleNamespace

import pytest

from raven import interaction, layout as L, log_workbench as W, pane_drag as P, workspace_layout
from raven.history_browser import Rect
from raven.logs import LogSession


def dashboard(monkeypatch, *, width=160, height=48, ascii_=False):
    sources = [dict(path="/first.out", label="stdout", lines=["stdout first", "stdout second", "stdout third"], size=40),
               dict(path="/second.err", label="stderr", lines=["stderr first", "stderr second", "stderr third"], size=40)]
    logs = LogSession()
    logs.path = sources[0]["path"]
    logs.entry = {"path": logs.path, "label": "stdout"}
    logs.entries = [{"id": "scheduler.stdout", "path": sources[0]["path"], "label": "stdout"},
                    {"id": "scheduler.stderr", "path": sources[1]["path"], "label": "stderr"}]
    instance = SimpleNamespace(tab="log", mode="main", width=width, height=height,
                               body_origin=7, logs=logs, toolbar_state={}, saves=0, messages=[], cfg={})
    instance.say = instance.messages.append
    instance.save = lambda: setattr(instance, "saves", instance.saves + 1)
    W.initialize(instance)
    interaction.initialize(instance)
    instance.log_workbench_state["view"] = "split"
    monkeypatch.setattr(W, "_alternate", lambda app: {"sources": sources})
    views = SimpleNamespace(g=L.Glyphs(ascii_))
    P.begin_frame(instance, width, height)
    return instance, views, sources


@pytest.mark.parametrize("ascii_", [False, True])
def test_log_source_drag_uses_actual_separator_and_keeps_presented_rows(monkeypatch, ascii_):
    app, views, sources = dashboard(monkeypatch, ascii_=ascii_)
    first = W.overlay(views, {}, app, 160, 48)
    divider = app.pane_drag_state["dividers"]["log:sources"]
    assert not divider.full_vertical
    assert "◆" not in "\n".join(L.row_text(row) for _, _, row in first)
    before = list(app.log_workbench_state["rows"])
    path = app.logs.path
    assert P.handle_mouse(app, divider.y, divider.x + 1, "press")
    assert P.handle_mouse(app, divider.y, divider.x + 16, "motion")
    assert app.log_workbench_state["split_ratio"] == 60 and app.saves == 0
    P.begin_frame(app, 160, 48)
    second = W.overlay(views, {}, app, 160, 48)
    resized = app.pane_drag_state["dividers"]["log:sources"]
    assert resized.x > divider.x
    assert P.handle_mouse(app, divider.y, divider.x + 16, "release")
    assert app.saves == 1 and app.logs.path == path
    assert app.log_workbench_state["rows"] == before
    assert "stdout first" in "\n".join(L.row_text(row) for _, _, row in second)
    assert "stderr first" in "\n".join(L.row_text(row) for _, _, row in second)
    if ascii_:
        assert all(L.row_text(row).isascii() for _, _, row in second)


@pytest.mark.parametrize("rect", [Rect(0, 7, 120, 40), Rect(40, 7, 120, 40),
                                  Rect(0, 7, 160, 25), Rect(0, 17, 160, 30)])
def test_docked_log_overlay_and_all_hits_stay_inside_content_rect(monkeypatch, rect):
    app, views, sources = dashboard(monkeypatch)
    app.history_browser_state = {"frame": {"tab": "log", "mode": "main", "geometry": (160, 48)}}
    app.history_browser_content_rect = rect
    rendered = W.overlay(views, {}, app, 160, 48)
    assert rendered
    for y, x, row in rendered:
        assert rect.x <= x and x + L.vlen(L.row_text(row)) <= rect.x + rect.width
        assert rect.y <= y < rect.y + rect.height
    for y, kind, value in app.log_workbench_state["control_hits"]:
        assert rect.y <= y < rect.y + rect.height
        assert rect.x <= value["left"] < value["right"] <= rect.x + rect.width
    divider = app.pane_drag_state["dividers"]["log:sources"]
    assert rect.x <= divider.x < rect.x + rect.width
    assert rect.y <= divider.y < divider.y + divider.height <= rect.y + rect.height
    for y, (left, right, index) in app.log_workbench_state["mouse_rows"].items():
        assert rect.y <= y < rect.y + rect.height
        assert rect.x <= left < right <= rect.x + rect.width


def test_short_docked_log_keeps_data_and_copy_source_buttons_visible(monkeypatch):
    app, views, sources = dashboard(monkeypatch)
    app.history_browser_state = {"frame": {"tab": "log", "mode": "main", "geometry": (160, 48)}}
    app.history_browser_content_rect = Rect(0, 7, 160, 12)
    rendered = W.overlay(views, {}, app, 160, 48)
    text = "\n".join(L.row_text(row) for _, _, row in rendered)
    assert "stdout first" in text and "stderr first" in text
    actions = [value["action"] for _, _, value in app.log_workbench_state["control_hits"]]
    assert ("key", "[") in actions and ("key", "]") in actions and ("key", "esc") in actions


def test_short_and_long_log_publications_do_not_move_the_splitter_origin(monkeypatch):
    app, views, sources = dashboard(monkeypatch)
    W.overlay(views, {}, app, 160, 48)
    first = app.pane_drag_state["dividers"]["log:sources"]
    sources[1]["lines"] = ["stderr expanded " + "x" * 500] * 3
    sources[1]["path"] = "/very/long/registered/path/" + "project/" * 30 + "stderr.log"
    W.overlay(views, {}, app, 160, 48)
    second = app.pane_drag_state["dividers"]["log:sources"]
    assert first.x == second.x and first.origin == second.origin and first.extent == second.extent


@pytest.mark.parametrize("stale", ["tab", "mode", "geometry", "malformed"])
def test_stale_history_geometry_does_not_offset_other_log_presentations(monkeypatch, stale):
    app, views, sources = dashboard(monkeypatch)
    frame = {"tab": "log", "mode": "main", "geometry": (160, 48)}
    app.history_browser_state = {"frame": frame}
    app.history_browser_content_rect = Rect(40, 7, 120, 40)
    if stale == "tab":
        frame["tab"] = "research"
    elif stale == "mode":
        frame["mode"] = "analysis"
    elif stale == "geometry":
        frame["geometry"] = (80, 24)
    else:
        app.history_browser_state = None
    assert W._content_viewport(app, 160, 48) == (0, 0, 160, 48)


def test_changed_split_ratio_persists_without_worker_state_or_sources(monkeypatch):
    app, views, sources = dashboard(monkeypatch)
    assert "split_ratio" not in W.save(app)
    W.restore(app, {"split_ratio": 65})
    assert W.save(app)["split_ratio"] == 65
    assert not any(key in W.save(app) for key in ("cache", "sources", "diff_sources", "rows"))
    W.restore(app, {"split_ratio": 1000})
    assert W.save(app)["split_ratio"] == 80
    W.restore(app, {"split_ratio": True})
    assert W.save(app)["split_ratio"] == 80


def test_pair_resize_preserves_raw_yank_selection_and_horizontal_pan(monkeypatch, tmp_path):
    app, views, sources = dashboard(monkeypatch)
    path = tmp_path / "original.out"
    raw = b"first\tcolumn\r\nsecond\r\n"
    path.write_bytes(raw)
    app.logs.path = str(path)
    buf = app.logs.buffer(str(path))
    app.logs.begin_selection(buf, 0)
    app.logs.move_cursor("end", buf)
    app.log_workbench_state["pan"] = 8
    W.overlay(views, {}, app, 160, 48)
    divider = app.pane_drag_state["dividers"]["log:sources"]
    P.handle_mouse(app, divider.y, divider.x, "press")
    P.handle_mouse(app, divider.y, divider.x - 15, "motion")
    P.handle_mouse(app, divider.y, divider.x - 15, "release")
    assert app.logs.selection_bytes(buf) == raw
    assert app.logs.path == str(path) and app.log_workbench_state["pan"] == 8


def test_nested_workspace_divider_is_registered_absolute_but_returns_local_hits(monkeypatch):
    app, views, sources = dashboard(monkeypatch, width=240)
    app.tab = "analytics"
    app.cfg = {"workspace": {"density": "comfortable"}}
    app.history_browser_state = {"frame": {"tab": "analytics", "mode": "main", "geometry": (240, 48)}}
    app.history_browser_content_rect = Rect(40, 12, 200, 35)
    groups = {"main": {"rows": [[(" Data", "")]], "hits": []},
              "details": {"rows": [[(" Evidence", "")]], "hits": []}}
    P.begin_frame(app, 240, 48)
    rows, hits = workspace_layout.transform_body(app, [], [], 200, 35, groups=groups)
    divider = app.pane_drag_state["dividers"]["workspace:analytics"]
    assert divider.x == 159 and divider.y == 12 and divider.origin == 40
    assert not divider.full_vertical
    y, kind, value = next(hit for hit in hits if hit[1] == "control")
    assert y == 17 and value["left"] == 119 and value["right"] == 120
    assert L.row_text(rows[y])[119] == "│"
    assert app.workspace_main_rect.x == 40 and app.workspace_main_rect.y == 12


def test_log_split_render_and_motion_use_only_published_data(monkeypatch):
    app, views, sources = dashboard(monkeypatch)
    def forbidden(*args, **kwargs):
        raise AssertionError("Log resize must not read files")
    monkeypatch.setattr("builtins.open", forbidden)
    W.overlay(views, {}, app, 160, 48)
    divider = app.pane_drag_state["dividers"]["log:sources"]
    P.handle_mouse(app, divider.y, divider.x, "press")
    for x in range(divider.x - 10, divider.x + 10):
        assert P.handle_mouse(app, divider.y, x, "motion")
    assert P.handle_key(app, "esc")
