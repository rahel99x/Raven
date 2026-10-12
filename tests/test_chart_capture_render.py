"""Fresh report observations paint in the coordinate system of a held drag."""
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, charts, layout as L


@pytest.fixture
def app():
    value = SimpleNamespace(mode="main", tab="analytics", width=120, height=40,
                            selected_id="7", analytics_job="7", analytics_view="job",
                            toolbar_state={}, project_state={}, job_panel_state={},
                            research=SimpleNamespace(generation=3), messages=[])
    value.say = value.messages.append
    A.initialize(value)
    return value


def draw_report(app, points, *, interactive=False, ascii_=False):
    C.begin_frame(app, app.width, app.height)
    metadata = {}
    identity = C.key(app, "loss", "/project/job-7/metrics.jsonl", "7",
                     scope="reported-metric", attempt="first")
    rows = A.chart_rows(L.Glyphs(ascii_), app, points, 100, 8, "loss",
                        "/project/job-7/metrics.jsonl", interactive=interactive,
                        metadata=metadata, zoom_key=identity)
    C.record(app, identity, metadata, scale=metadata["scale"])
    plots = C.publish(app, app.width, app.height)
    return rows, next(plot for plot in plots if plot.key == identity)


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("axis", ["auto", "log"])
def test_report_append_and_correction_keep_drag_axes_without_freezing_observations(app, interactive, ascii_, axis):
    points = [{"t": float(i), "value": 1. + i / 10} for i in range(20)]
    app.analysis_state["axes"]["loss"] = {"mode": axis}
    rows, original = draw_report(app, points, interactive=interactive, ascii_=ascii_)
    assert C.handle_mouse(app, original.visible.top + 1, original.visible.left + 3, button="press")
    old_header = L.row_text(rows[0])
    # A corrected observation in the retained interval must be visible, while
    # a new observation past its end must not change the chart's transform.
    points[10]["value"] = 9.
    points.append({"t": 20., "value": 1000.})
    changed, current = draw_report(app, points, interactive=interactive, ascii_=ascii_)
    assert C.active(app)
    assert current.rect == original.rect
    assert current.x_bounds == original.x_bounds and current.y_bounds == original.y_bounds
    assert len(changed) == len(rows)
    assert L.row_text(changed[0]) != old_header
    assert "1.0k" not in L.row_text(changed[0])
    C.cancel(app)
    _, refreshed = draw_report(app, points, interactive=interactive, ascii_=ascii_)
    assert refreshed.x_bounds[1] == 20.
    assert refreshed.y_bounds[1] > original.y_bounds[1]


@pytest.mark.parametrize("ascii_", [False, True])
def test_report_cache_separates_held_axes_and_resumes_current_bounds_after_cancel(app, ascii_):
    points = [{"t": float(i), "value": 1. + i / 10} for i in range(20)]
    _, original = draw_report(app, points, ascii_=ascii_)
    C.handle_mouse(app, original.visible.top + 1, original.visible.left + 3, button="press")
    points.append({"t": 20., "value": 1000.})
    pinned_rows, pinned = draw_report(app, points, ascii_=ascii_)
    assert C.active(app) and pinned.x_bounds == original.x_bounds
    # Same immutable content can reuse its card while the gesture is held.
    cached_rows, cached = draw_report(app, points, ascii_=ascii_)
    assert cached_rows == pinned_rows and cached.x_bounds == pinned.x_bounds
    C.cancel(app)
    current_rows, current = draw_report(app, points, ascii_=ascii_)
    assert current_rows != pinned_rows and current.x_bounds[1] == 20.
    assert "1.0k" in L.row_text(current_rows[0])


@pytest.mark.parametrize("interactive", [False, True])
def test_captured_zoom_paints_current_data_without_repeating_its_y_fit(app, monkeypatch, interactive):
    points = [{"t": float(i), "value": 1. + i / 10} for i in range(20)]
    _, full = draw_report(app, points, interactive=interactive)
    C._apply(app, full, {"x": (3., 15.), "y": full.y_bounds, "fit_y": True})
    _, original = draw_report(app, points, interactive=interactive)
    C.handle_mouse(app, original.visible.top + 1, original.visible.left + 3, button="press")
    points[10]["value"] = 1000.
    monkeypatch.setattr(charts, "fit_time_bounds", lambda *args: pytest.fail("held axes repeated a Y-fit scan"))
    rows, current = draw_report(app, points, interactive=interactive)
    assert C.active(app) and current.y_bounds == original.y_bounds
    assert "1.0k" in L.row_text(rows[0])


@pytest.mark.parametrize("interactive", [False, True])
def test_held_report_survives_bounded_retention_without_manufacturing_samples(app, interactive):
    points = [{"t": float(i), "value": 1. + i / 10} for i in range(20)]
    _, original = draw_report(app, points, interactive=interactive)
    y = original.visible.top + 1
    C.handle_mouse(app, y, original.visible.left + 3, button="press")
    points[:] = [{"t": 100. + i, "value": 900.} for i in range(20)]
    rows, current = draw_report(app, points, interactive=interactive)
    assert C.active(app)
    assert current.kind == "metric-empty"
    assert current.x_bounds == original.x_bounds and current.y_bounds == original.y_bounds
    assert "900" not in L.row_text(rows[0])
    assert "0/20 samples" in L.row_text(rows[-1])
    C.handle_mouse(app, y, original.visible.right - 3, button="release")
    assert not C.active(app) and C.bounds(app, original.key) is not None
    _, zoomed = draw_report(app, points, interactive=interactive)
    assert zoomed.kind == "metric-empty"


@pytest.mark.parametrize("ascii_", [False, True])
def test_aligned_job_comparison_pins_only_captured_job_axes(app, ascii_):
    from raven.model import Job, Store
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "train", "cpu", "RUNNING", cpus=4) for jid in (7, 8)]
    for job in store.jobs:
        for i in range(20):
            store.record(job.id, {"k": "live", "t": 100. + i, "cpu": .5,
                                   "rss": 1024 ** 3})
    app.store = store
    app.job_record = lambda jid, snap: next(job for job in store.jobs if job.id == jid)
    app.mode = "analysis"
    app.analysis_state.update(modal="diff", diff_ids=["7", "8"])

    def draw():
        C.begin_frame(app, app.width, 100)
        rows = A._job_diff_rows(L.Glyphs(ascii_), store.snapshot(), app, 100)
        return rows, C.publish(app, app.width, 100)

    _, plots = draw()
    original = next(plot for plot in plots if plot.key[1:3] == ("7", "Memory (GB)"))
    other = next(plot for plot in plots if plot.key[1:3] == ("8", "Memory (GB)"))
    C.handle_mouse(app, original.visible.top + 1, original.visible.left + 3, button="press")
    store.record("7", {"k": "live", "t": 120., "cpu": .5, "rss": 100 * 1024 ** 3})
    _, updated = draw()
    current = next(plot for plot in updated if plot.key == original.key)
    current_other = next(plot for plot in updated if plot.key == other.key)
    assert C.active(app)
    assert current.rect == original.rect
    assert current.x_bounds == original.x_bounds and current.y_bounds == original.y_bounds
    assert current_other.x_bounds != other.x_bounds


@pytest.mark.parametrize("change", ["job", "attempt", "source", "scale", "geometry"])
def test_report_genuine_mapping_changes_cancel_instead_of_pinning_other_sources(app, change):
    points = [{"t": float(i), "value": 1. + i / 10} for i in range(20)]
    _, original = draw_report(app, points)
    C.handle_mouse(app, original.visible.top + 1, original.visible.left + 3, button="press")
    if change == "job":
        app.selected_id = app.analytics_job = "8"
    elif change == "attempt":
        app.project_state = {"binding": {"job_id": "7", "attempt": "next"}}
    elif change == "source":
        app.research.generation += 1
    elif change == "scale":
        app.analysis_state["axes"]["loss"] = {"mode": "log"}
    else:
        app.width -= 10
    draw_report(app, points)
    assert not C.active(app)
    C.handle_mouse(app, original.visible.top + 1, original.visible.right - 3, button="release")
    assert C.bounds(app, original.key) is None


@pytest.mark.parametrize("painter", [charts.braille_chart, charts.vbar_chart])
@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("title", ["", "Measured loss"])
@pytest.mark.parametrize("time_units", [False, True])
def test_axis_extent_contains_only_axes_not_title_or_selected_time_note(painter, ascii_, title, time_units):
    metadata = {}
    rows = painter(L.Glyphs(ascii_), [1., 2., 3.], 90, 6, title=title,
                   sample_times=[0., .002, .004], times=(0., .004),
                   time_units=time_units, fitted=True, metadata=metadata)
    top, left, bottom, right = metadata["plot_rect"]
    axis = metadata["axis_rect"]
    assert axis == (top, 3, bottom + 2, right)
    assert top == int(bool(title)) and axis[1] < left
    assert ("+" if ascii_ else "└") in L.row_text(rows[bottom])
    assert L.row_text(rows[axis[2] - 1]).strip()
    assert axis[2] == len(rows) - int(time_units)
    if time_units:
        assert "Time +offset" in L.row_text(rows[axis[2]])


@pytest.mark.parametrize("painter", [charts.braille_chart, charts.vbar_chart])
@pytest.mark.parametrize("ascii_", [False, True])
def test_timestamp_less_axis_extent_excludes_nonexistent_ticks(painter, ascii_):
    metadata = {}
    rows = painter(L.Glyphs(ascii_), [1., 2., 3.], 90, 6, metadata=metadata)
    assert metadata["axis_rect"] == (0, 3, 7, 90)
    assert len(rows) == metadata["axis_rect"][2]


@pytest.mark.parametrize("width", [0, 2, 8, 10, 12])
def test_narrow_axis_extent_does_not_escape_declared_width(width):
    metadata = {}
    charts.braille_chart(L.Glyphs(False), [1., 2.], width, 3,
                         sample_times=[0., 1.], times=(0., 1.), metadata=metadata)
    for name in ("plot_rect", "axis_rect"):
        top, left, bottom, right = metadata[name]
        assert 0 <= left <= right <= width and 0 <= top <= bottom
