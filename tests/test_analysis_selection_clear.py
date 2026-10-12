"""Right-click clears local row cursors without changing modal ownership."""
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, job_selection as S, layout as L, screen
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
    BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
    BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256, BUTTON_SHIFT=512)


@pytest.fixture
def dashboard():
    cfg = Config({"animations": False, "log_lines": 0,
                  "clipboard": {"tools": False, "osc52": False}})
    store = Store(persist=False)
    store.jobs = [Job("7", "train", "cpu", "RUNNING", cpus=4)]
    for index in range(12):
        store.record("7", {"k": "live", "t": 100. + index, "cpu": .5,
                           "rss": 1024 ** 3})
    store.events.extend({"t": 101. + index, "kind": "started", "job": "7",
                         "text": f"event {index}"} for index in range(12))
    app = App(store, None, None, cfg, "tester", interactive=False)
    app.width, app.height = 120, 40
    app.selected_id = app.analytics_job = app.research_job_id = "7"
    app.marks = {"7", "8"}
    app.mode = "analysis"
    app.analysis_state.update(modal="timeline", cursor=3, chart_job="7")
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    yield app, views, store
    if app.research:
        app.research.close()


def draw(dashboard):
    app, views, store = dashboard
    C.begin_frame(app, app.width, app.height)
    rows = A.overlay(views, store.snapshot(), app, app.width, app.height)
    C.publish(app, app.width, app.height)
    return rows


def selected_rows(rows):
    return [row for _, _, row in rows for _, style in row if "rev" in style]


@pytest.mark.parametrize("modal", ["timeline", "chart_events"])
@pytest.mark.parametrize("route", ["controller", "screen"])
def test_local_event_clear_preserves_sources_and_marks_without_pointer_io(dashboard, monkeypatch, modal, route):
    app, views, store = dashboard
    app.analysis_state["modal"] = modal
    assert selected_rows(draw(dashboard))
    sources = (app.selected_id, app.analytics_job, app.research_job_id)
    app.sel_anchor, app.sel_end, app.click_row = 10, 14, 14
    with monkeypatch.context() as pointer:
        pointer.setattr(store, "snapshot", lambda: pytest.fail("right-click took a scheduler snapshot"))
        pointer.setattr(views, "compose", lambda *args: pytest.fail("right-click rendered the source"))
        pointer.setattr("builtins.open", lambda *args, **kwargs: pytest.fail("right-click opened a file"))
        if route == "controller":
            app.click(0, 0, [], button="right")
        else:
            screen._apply_input(app, ("mouse", (0, 0, 0, 0, MOUSE.BUTTON3_CLICKED)), [], MOUSE)
    assert not A.rows_selected(app, modal)
    assert app.mode == "analysis" and app.analysis_state["modal"] == modal
    assert (app.selected_id, app.analytics_job, app.research_job_id) == sources
    assert app.marks == {"7", "8"}
    assert app.sel_anchor is None and app.click_row is None
    for _ in range(3):
        store.events.append({"t": 130., "kind": "started", "job": "7", "text": "new event"})
        assert not selected_rows(draw(dashboard))


@pytest.mark.parametrize("modal", ["timeline", "chart_events"])
def test_hidden_event_enter_and_replay_seek_are_inert_until_local_arrow(dashboard, monkeypatch, modal):
    app, _, store = dashboard
    app.analysis_state["modal"] = modal
    draw(dashboard)
    app.click(0, 0, [], button="right")
    opened = []
    monkeypatch.setattr(A, "_open_timeline_event", lambda *args, **kwargs: opened.append(args[1]))
    with monkeypatch.context() as pointer:
        pointer.setattr(store, "snapshot", lambda: pytest.fail("hidden Enter read a snapshot"))
        assert A.handle_key(app, "enter")
        if modal == "timeline":
            assert A.handle_key(app, "s")
    assert opened == []
    assert A.handle_key(app, "down")
    assert A.rows_selected(app, modal)
    assert S.selected(app, "jobs", "7") == "7"
    assert A.handle_key(app, "enter") and len(opened) == 1


@pytest.mark.parametrize("modal", ["timeline", "chart_events"])
def test_cleared_event_list_scroll_stays_independent_of_hidden_cursor(dashboard, modal):
    app, _, _ = dashboard
    app.height = 20
    app.analysis_state["modal"] = modal
    draw(dashboard)
    app.click(0, 0, [], button="right")
    app.analysis_state["scroll"] = 8
    draw(dashboard)
    assert app.analysis_state["scroll"] == 8
    assert not A.rows_selected(app, modal)


@pytest.mark.parametrize("modal", ["timeline", "chart_events"])
def test_exact_event_row_click_resumes_only_local_cursor(dashboard, monkeypatch, modal):
    app, _, _ = dashboard
    app.analysis_state["modal"] = modal
    S.initialize(app)["deselected"]["jobs"] = True
    draw(dashboard)
    app.click(0, 0, [], button="right")
    draw(dashboard)
    y, _, control = next(hit for hit in app.analysis_state["control_hits"] if "event" in hit[2])
    opened = []
    monkeypatch.setattr(A, "_open_timeline_event", lambda *args, **kwargs: opened.append(args[1]))
    assert A.handle_mouse(app, y, control["left"], button="left")
    assert opened == [control["event"]]
    assert A.rows_selected(app, modal)
    assert S.selected(app, "jobs", "7") is None


@pytest.mark.parametrize("modal", ["timeline", "chart_events"])
def test_cleared_event_graph_focus_cannot_bypass_hidden_enter_guard(dashboard, monkeypatch, modal):
    from raven import interaction
    app, _, store = dashboard
    app.tab = "analytics"
    app.analysis_state["modal"] = modal
    rows = draw(dashboard)
    graph = interaction.publish(app, [], [], app.width, app.height, overlays=rows)
    focus = next(control for control in graph.controls if control.group in ("timeline_events", "chart_events"))
    interaction.initialize(app).update(focused=focus.id, active=True)
    app.click(0, 0, [], button="right")
    assert not interaction.initialize(app)["active"]
    assert interaction.initialize(app)["focused"] is None
    monkeypatch.setattr(A, "_open_timeline_event", lambda *args, **kwargs: pytest.fail("hidden event opened through graph focus"))
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("hidden event Enter took a snapshot"))
    app.handle("enter")


def test_chart_outside_right_clears_sample_and_range_but_retains_zoom(dashboard):
    app, _, _ = dashboard
    app.analysis_state.update(modal="chart", metric="CPU per core (%)")
    draw(dashboard)
    plot = C.initialize(app)["plots"][0]
    C._apply(app, plot, {"x": (102., 108.), "y": plot.y_bounds, "fit_y": True})
    app.analysis_state["chart_range"] = {"metric": "CPU per core (%)", "start": 102., "end": 105.}
    draw(dashboard)
    saved = C.bounds(app, plot.key)
    app.click(0, 0, [], button="right")
    assert C.bounds(app, plot.key) == saved
    assert not A.rows_selected(app, "chart") and "chart_range" not in app.analysis_state
    rendered = "\n".join(L.row_text(row) for _, _, row in draw(dashboard))
    assert " value 50.0" not in rendered and "Range t=" not in rendered
    assert A.handle_key(app, "r") and "chart_range" not in app.analysis_state
    A.handle_key(app, "right")
    assert A.rows_selected(app, "chart")
    rendered = "\n".join(L.row_text(row) for _, _, row in draw(dashboard))
    assert " value 50.0" in rendered


def test_graph_reset_has_priority_over_local_sample_clear(dashboard):
    app, _, _ = dashboard
    app.analysis_state.update(modal="chart", metric="CPU per core (%)")
    draw(dashboard)
    plot = C.initialize(app)["plots"][0]
    C._apply(app, plot, {"x": (102., 108.), "y": plot.y_bounds, "fit_y": True})
    draw(dashboard)
    plot = C.initialize(app)["plots"][0]
    app.click(plot.visible.top + 1, plot.visible.left + 2, [], button="right")
    assert C.bounds(app, plot.key) is None
    assert A.rows_selected(app, "chart") and app.marks == {"7", "8"}


@pytest.mark.parametrize("modal", ["inspect", "diff", "dashboard"])
def test_static_dialog_right_click_preserves_current_view_and_sources(dashboard, modal):
    app, _, _ = dashboard
    app.analysis_state.update(modal=modal, job="7", section=2, diff_ids=["7", "8"])
    before = {key: app.analysis_state.get(key) for key in ("modal", "job", "section", "diff_ids")}
    assert A.context_click(app, 0, 0, button="right")
    assert {key: app.analysis_state.get(key) for key in before} == before
    assert app.mode == "analysis" and app.marks == {"7", "8"}


@pytest.mark.parametrize("mode", ["confirm", "history_log_export", "log_tools_picker", "palette", "main"])
def test_local_dialog_handler_does_not_consume_other_modes(dashboard, mode):
    app, _, _ = dashboard
    app.mode = mode
    assert not A.context_click(app, 0, 0, button="right")
    assert "rows_deselected" not in app.analysis_state


@pytest.mark.parametrize("toolbar", [{"menu": "view"}, {"panel": "settings"}])
def test_local_dialog_handler_leaves_toolbar_overlay_ownership(dashboard, toolbar):
    app, _, _ = dashboard
    app.toolbar_state = toolbar
    assert not A.context_click(app, 0, 0, button="right")
    assert "rows_deselected" not in app.analysis_state


def test_research_evidence_clear_hides_rows_and_enter_until_local_arrow(dashboard, monkeypatch):
    from raven.research_views import render
    app, views, store = dashboard
    app.mode, app.tab, app.research_view = "main", "research", "evidence"
    result = {"evidence": [{"id": "e1", "source": "log", "text": "failure", "path": "/logs/job-7.log", "line": 3}]}
    app.research = SimpleNamespace(context=lambda snap, target: {"jid": "7", "job": store.jobs[0]},
                                  request=lambda context: result, close=lambda: None)
    rows, _ = render(views, store.snapshot(), app, 120, 40)
    assert any("rev" in style for row in rows for _, style in row)
    app.click(0, 0, [], button="right")
    rows, _ = render(views, store.snapshot(), app, 120, 40)
    assert not any("rev" in style for row in rows for _, style in row)
    opened = []
    from raven import log_workbench
    monkeypatch.setattr(log_workbench, "open_citation", lambda *args: opened.append(args[1]))
    A.handle_key(app, "enter")
    assert opened == []
    A.handle_key(app, "down")
    assert A.rows_selected(app, "evidence") and S.selected(app, "research", "7") is None
    A.handle_key(app, "enter")
    assert opened and opened[0]["id"] == "e1"


def test_research_array_clear_hides_cohort_and_task_selection_until_deliberate_arrow(dashboard):
    from raven import arrays
    from raven.research_views import render
    app, views, store = dashboard
    app.mode, app.tab, app.research_view = "main", "research", "arrays"
    records = [Job(f"{base}_{index}", f"array-{base}", "cpu", "RUNNING")
               for base in range(70, 78) for index in range(2)]
    groups = arrays.summarize(records)
    app.research = SimpleNamespace(context=lambda snap, target: {"jid": "7", "job": store.jobs[0]},
                                  request=lambda context: {"groups": groups}, close=lambda: None)
    app.research_array_open = True
    rows, _ = render(views, store.snapshot(), app, 120, None)
    assert any("rev" in style for row in rows for _, style in row)
    assert "task page / offset" in "\n".join(L.row_text(row) for row in rows)
    app.click(0, 0, [], button="right")
    rows, _ = render(views, store.snapshot(), app, 120, None)
    assert not any("rev" in style for row in rows for _, style in row)
    assert "task page / offset" not in "\n".join(L.row_text(row) for row in rows)
    assert app.research_job_id == "7"
    was_open = app.research_array_open
    app.handle("enter")
    assert app.research_array_open == was_open
    app.research_scroll, app.research_array_focus = 4, True
    render(views, store.snapshot(), app, 120, 12)
    assert app.research_scroll == 4
    app.handle("down")
    rows, _ = render(views, store.snapshot(), app, 120, None)
    assert any("rev" in style for row in rows for _, style in row)
    assert "task page / offset" in "\n".join(L.row_text(row) for row in rows)
