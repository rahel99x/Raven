"""Visible inline metric bands retain full document geometry after navigation."""
import math
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, clock, layout as L, scrollbars as S
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


def test_cancelled_scroll_peek_matches_next_viewport_after_motion_is_rearmed():
    from raven import scrolling as S
    app = SimpleNamespace(cfg={}, animations_enabled=True, theme="default")
    key, context = "workspace:jobs:details", ("7", 160, 43, "analytics", "job")
    assert S.viewport(app, key, 0, 201, 43, context=context, now=10.) == 0
    S.note_input(app, "capture")
    pid = S.initialize(app)["controllers"][key]["pid"]
    assert pid.updated is None
    # A wheel report may arrive after the scrollbar jump cancelled the prior
    # PID clock. The renderer's read-only peek must match the imminent snap.
    S.note_input(app, "wheel", now=10.1)
    prepared = S.published_position(app, key, 158, context=context, now=10.1)
    assert prepared == 158 and pid.updated is None
    assert S.viewport(app, key, 158, 201, 43, context=context, now=10.1) == prepared


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("wide", [180, 320])
def test_inline_virtual_series_matches_complete_render_after_scroll_resize_and_density(monkeypatch, ascii_, wide):
    cfg = Config({"animations": False, "startup": {"enabled": False}, "log_lines": 0,
                  "smooth_scrolling": False, "workspace": {"density": "compact"}})
    store = Store(persist=False)
    store.apply_jobs([Job("7", "multiple-device-training", "gpu", "RUNNING", cpus=16,
                          mem_req="32G", gpus=4)])
    for index in range(100):
        store.record("7", {"k": "live", "t": 1000. + index,
                           "cpu": .5 + .4 * math.sin(index / 20),
                           "rss": (2 + math.sin(index / 30)) * (1 << 30)})
        store.record("7", {"k": "gpu", "t": 1000. + index,
                           "gpu": {str(device): [50 + 40 * math.sin(index / 20 + device)]
                                   for device in range(4)}})
    store.trace["7"] = [{"index": device, "t": 1000. + index * 60,
                         "util": 50 + 40 * math.sin(index / 20 + device)}
                        for device in range(4) for index in range(100)]
    monkeypatch.setattr(clock, "now", lambda: 7000.)
    dashboards = []
    for interactive in (False, True):
        app = App(store, None, None, cfg, "test", interactive=interactive)
        app.selected_id = app.analytics_job = "7"
        app.job_panel_state.update(mode="analytics", analytics_view="job")
        views = Views(L.Glyphs(ascii_), cfg)
        app.views_ref = views
        dashboards.append((app, views))

    def compare(width, density, action=None):
        frames = []
        for app, views in dashboards:
            app.layout_state.density = density
            if action is not None:
                from raven import workspace_layout as W
                old_focus = app.layout_state.focus
                app.layout_state.focus = "details"
                W._scroll(app, action)
                app.layout_state.focus = old_focus
            rows, _ = views.compose(store.snapshot(), app, width, 50)
            plots = C.initialize(app)["plots"]
            pane = next(value for value in S.initialize(app)["panes"]
                        if value.key == "workspace:jobs:details")
            frames.append((rows, [(plot.key, plot.rect, plot.visible, plot.x_bounds, plot.y_bounds)
                                  for plot in plots], (pane.count, pane.page, pane.target)))
            assert plots, "A visible metric viewport must publish its source hit map on the first frame"
            assert all(0 <= plot.visible.top < plot.visible.bottom <= 50 and
                       0 <= plot.visible.left < plot.visible.right <= width for plot in plots)
        # The noninteractive reader composes all bands. It supplies an
        # independent reference for the virtual viewport's rows and hit map.
        assert frames[0] == frames[1]

    try:
        compare(wide, "compact")
        compare(wide, "compact", "pgdn")
        compare(wide, "compact", "end")
        compare(100, "compact")
        compare(wide, "comfortable")
        compare(wide, "comfortable", "home")
        compare(wide, "comfortable", "end")
        # A new publication may remove whole metric bands. The retained
        # offset is clamped against the new document on that same frame.
        live = [sample for sample in store.series["7"] if sample["k"] == "live"]
        store.series["7"].clear()
        store.series["7"].extend(live)
        store.trace.pop("7")
        store.jobs[0].gpus = 0
        compare(wide, "comfortable")
    finally:
        for app, _ in dashboards:
            if app.research:
                app.research.close()
