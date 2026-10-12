"""Offscreen telemetry summaries retain geometry without preparing their data."""
import pytest

from raven import analytics_document as D, chart_interaction as C, charts, clock
from raven import layout as L, scrollbars as S, views as V, workspace_layout as W
from test_analytics_document import native


@pytest.mark.parametrize("surface", ["analytics", "jobs"])
def test_cold_hidden_heatmap_does_no_raster_or_value_work_and_returns_correctly(native, monkeypatch, surface):
    app, views, store = native
    # Keep selection deterministic in both workspaces. Four native GPU devices
    # and four trace devices make the bottom viewport independent of the first
    # four native vectors used by the telemetry summary.
    store.jobs = store.jobs[:1]
    app.tab = surface
    app.interactive = True
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    monkeypatch.setattr(clock, "now", lambda: 100.)
    native_values, materialized, heatmaps = set(), [], []
    prepare = V._prepare_native_metrics
    def observe_preparation(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        native_values.clear()
        native_values.update(id(plot[1]) for plot in prepared["plots"])
        return prepared
    materialize = V._MetricValues.materialize
    def observe_materialization(values):
        if id(values) in native_values:
            materialized.append(id(values))
        return materialize(values)
    heatmap = charts.heatmap
    def observe_heatmap(*args, **kwargs):
        heatmaps.append(1)
        return heatmap(*args, **kwargs)
    monkeypatch.setattr(V, "_prepare_native_metrics", observe_preparation)
    monkeypatch.setattr(V._MetricValues, "materialize", observe_materialization)
    monkeypatch.setattr(charts, "heatmap", observe_heatmap)

    def draw():
        rows, _ = views.compose(store.snapshot(), app, 320, 50)
        plots = C.initialize(app)["plots"]
        geometry = [(plot.key, plot.rect, plot.visible) for plot in plots]
        if surface == "analytics":
            frame = D.initialize(app)["frame"]
            viewport = frame["count"], frame["page"], frame["painted"]
        else:
            pane = next(pane for pane in S.initialize(app)["panes"] if pane.key == "workspace:jobs:details")
            viewport = pane.count, pane.page, pane.target
        return rows, plots, geometry, viewport

    def scroll(action):
        if surface == "analytics":
            assert D.scroll(app, action)
        else:
            app.layout_state.focus = "details"
            W._scroll(app, action)

    top_rows, _, top_geometry, top_viewport = draw()
    assert heatmaps and materialized
    assert "telemetry heatmap" in L.to_text(top_rows, 320)
    scroll("end")
    _, bottom_plots, bottom_geometry, bottom_viewport = draw()
    assert bottom_plots and all(plot.key[2].startswith("gpu-trace:") for plot in bottom_plots)
    assert bottom_viewport[2] > 0
    materialized.clear()
    heatmaps.clear()

    # A fresh native publication invalidates its summary cache. Skipping only
    # a previously cached raster would not satisfy this case.
    store.record("900", {"k": "live", "t": 6., "cpu": .9, "rss": 1024 ** 3})
    store.record("900", {"k": "gpu", "t": 6.,
                         "gpu": {f"node:{index}": [60, 1, 2] for index in range(4)}})
    hidden_rows, hidden_plots, hidden_geometry, hidden_viewport = draw()
    assert not heatmaps
    assert not materialized, "Hidden native cards and heatmap must leave their lazy vectors unmaterialized"
    assert hidden_geometry == bottom_geometry
    assert hidden_viewport == bottom_viewport
    assert "telemetry heatmap" not in L.to_text(hidden_rows, 320)

    scroll("home")
    restored_rows, _, restored_geometry, restored_viewport = draw()
    assert len(heatmaps) == 1 and materialized
    assert "telemetry heatmap" in L.to_text(restored_rows, 320)
    assert restored_geometry == top_geometry
    assert restored_viewport == top_viewport
