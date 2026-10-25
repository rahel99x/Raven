"""A reserved navigation column shares one coordinate system with native panes."""

from __future__ import annotations

import pytest

from raven import chart_interaction, layout as L, sidebar, terminal_ui, workspace_layout
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.ui.content_geometry import shift_hit
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"animations": False, "startup": {"enabled": False}, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job("900", "exact-job", "cpu", "RUNNING", cpus=4)]
    app = App(store, None, None, cfg, "student", interactive=False)
    views = Views(L.Glyphs(True), cfg)
    app.views_ref = views
    yield app, views, store
    if app.research:
        app.research.close()


@pytest.mark.parametrize("width,height", [(1, 2), (24, 8), (40, 24), (80, 28), (120, 32), (160, 36)])
@pytest.mark.parametrize("tab", ["jobs", "history", "analytics", "research", "log", "traces", "nodes"])
def test_composed_sidebar_is_reserved_and_document_hits_have_global_coordinates(dashboard, width, height, tab):
    app, views, store = dashboard
    app.enter_tab(tab)
    rows, hits = views.compose(store.snapshot(), app, width, height)
    left = sidebar.width(app, width)
    assert app.width == width and app.content_origin_x == left
    assert len(rows) == height
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    assert all(0 <= left < right <= app.content_origin_x
               for _, left, right, _ in app.tab_hits)
    for y, kind, value in hits:
        assert 0 <= y < height - 1
        if isinstance(value, dict) and "left" in value:
            assert left <= value["left"] < value["right"] <= width
        elif kind == "sort_header":
            assert left <= value[2] < value[3] <= width
        elif kind in ("job_panel_tab", "job_panel_view", "job_panel_file", "job_panel_action", "node_cell"):
            assert left <= value[1] < value[2] <= width
    if height >= 24 and width >= 40:
        assert any("Jobs" in L.row_text(row)[:left] for row in rows)
    rect = getattr(app, "workspace_main_rect", None)
    if rect is not None and rect.width:
        assert left <= rect.x and rect.x + rect.width <= width


@pytest.mark.parametrize("tab", ["jobs", "analytics", "research"])
def test_terminal_native_bounds_do_not_cover_sidebar(dashboard, tab):
    app, views, store = dashboard
    app.enter_tab(tab)
    state = terminal_ui.initialize(app)
    state.modes[tab] = "terminal"
    rows, hits = views.compose(store.snapshot(), app, 160, 36)
    left = sidebar.width(app, 160)
    assert state.rect is not None
    assert left <= state.rect.x < state.rect.x + state.rect.width <= 160
    assert state.geometry == (160, 36)
    assert state.body_rect is None or state.body_rect.x >= left
    assert all(left <= control["left"] < control["right"] <= 160
               for _, kind, control in hits if kind == "control" and control.get("group", "").startswith("terminal"))


def test_native_plot_and_scrollbar_share_reserved_document_origin(dashboard, monkeypatch):
    app, views, store = dashboard
    app.enter_tab("traces")
    workspace_layout.initialize(app).density = "compact"

    def document(snap, owner, width, height):
        chart_interaction.record(owner, ("test", "cpu"),
            {"plot_rect": (0, 0, 3, 10), "x_bounds": (0., 1.), "y_bounds": (0., 1.)})
        return [[("plot", "text")] for _ in range(4)], []

    monkeypatch.setattr(views, "trace_workspace_tab", document)
    views.compose(store.snapshot(), app, 80, 28)
    plot = chart_interaction.initialize(app)["plots"][0]
    assert plot.rect.left == app.content_origin_x
    assert plot.rect.top == app.body_origin
    assert plot.visible.left >= app.content_origin_x


def test_stream_output_retains_full_width_without_a_fake_navigation_column(dashboard):
    app, views, store = dashboard
    rows, _ = views.compose(store.snapshot(), app, 80, None)
    assert app.content_origin_x == 0
    assert all(L.vlen(L.row_text(row)) <= 80 for row in rows)


def test_notification_header_clicks_use_the_same_content_origin_after_each_frame(dashboard):
    app, views, store = dashboard
    app.say("Accounting refresh is temporarily unavailable", level="warning")
    for _ in range(2):
        _, hits = views.compose(store.snapshot(), app, 80, 28)
        notices = app.notification_ui_state["status_hits"]
        assert notices
        assert min(value["left"] for _, _, value in notices) == app.content_origin_x
    y, _, value = notices[0]
    app.click(y, value["left"], hits)
    assert app.mode == "notifications"


@pytest.mark.parametrize("kind,value,expected", [
    ("job", "900", "900"),
    ("control", {"left": 1, "right": 5, "action": ("command", "cancel 900")},
     {"left": 18, "right": 22, "action": ("command", "cancel 900")}),
    ("sort_header", ("jobs", "name", 1, 5), ("jobs", "name", 18, 22)),
    ("job_panel_tab", ("trace", 1, 5), ("trace", 18, 22)),
])
def test_hit_transform_moves_only_geometry_without_mutating_actions(kind, value, expected):
    before = value.copy() if isinstance(value, dict) else value
    assert shift_hit(3, kind, value, dx=17, dy=4) == (7, kind, expected)
    assert value == before
