"""Buffered reported curves reveal only acquired segments, with exact input maps."""
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, clock, layout as L, metric_live as M
from raven.model import Job


@pytest.fixture
def report(monkeypatch):
    wall = [202.0]
    monkeypatch.setattr(clock, "now", lambda: wall[0])
    app = SimpleNamespace(selected_id="1", research_job_id="1", tab="research",
                          mode="main", width=100, height=40, cfg={}, project_state={},
                          research=SimpleNamespace(generation=0), toolbar_state={}, job_panel_state={},
                          store=SimpleNamespace(jobs=[Job("1", "training", "gpu", "RUNNING")]))
    state = A.initialize(app)
    state.update(chart_events=False, chart_job="1")
    key = A.chart_key(app, "loss", "/run/metrics.jsonl", interactive=False)
    M.set_running(app, key, True)
    points = [{"t": float(timestamp), "value": 1000. + timestamp, "step": timestamp}
              for timestamp in range(150, 201, 5)]
    return SimpleNamespace(app=app, wall=wall, key=key, points=points)


def draw(report, *, ascii_=False, interactive=False, running=True):
    metadata = {}
    rows = A.chart_rows(L.Glyphs(ascii_), report.app, report.points, 90, 8, "loss", "/run/metrics.jsonl",
                        interactive=interactive, zoom_key=report.key, metadata=metadata, running=running)
    return rows, metadata


def has_ink_in_each_column(rows, metadata, ascii_):
    top, left, bottom, right = metadata["plot_rect"]
    if ascii_:
        ink = set("/\\-:|+")
    else:
        ink = set(A.charts.BRAILLE[1:])
    return all(any(L.row_text(row)[column:column + 1] in ink for row in rows[top:bottom])
               for column in range(left, right))


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("interactive", [False, True])
def test_short_delayed_window_draws_complete_acquired_segment_without_inventing_samples(report, ascii_, interactive):
    assert M.set_enabled(report.app, report.key, True)
    assert M.set_delta(report.app, report.key, 1.)
    rows, metadata = draw(report, ascii_=ascii_, interactive=interactive)
    assert metadata["x_bounds"] == (191., 192.)
    assert metadata["y_bounds"][1] > 1192
    assert has_ink_in_each_column(rows, metadata, ascii_)
    assert "between acquired samples" in L.row_text(rows[0])
    assert "0/11 samples" in L.row_text(rows[-1])
    assert not any("value " in L.row_text(row) for row in rows)
    if interactive:
        assert report.app.analysis_state["chart_visible"]["points"] == []
    assert all(point["value"] == 1000. + point["t"] for point in report.points)


@pytest.mark.parametrize("live", [False, True])
def test_new_acquired_successor_is_used_beyond_delayed_end_but_never_beyond_real_now(report, monkeypatch, live):
    report.points.append({"t": 205., "value": 999999., "step": 205})
    if live:
        assert M.set_enabled(report.app, report.key, True)
        assert M.set_delta(report.app, report.key, 1.)
    captured = []
    original = A.charts.braille_chart

    def raster(*args, **kwargs):
        captured.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(A.charts, "braille_chart", raster)
    rows, metadata = draw(report)
    assert metadata["x_bounds"] == ((191., 192.) if live else (150., 192.))
    assert max(captured[-1]["sample_times"]) == 195.
    assert metadata["x_bounds"][1] < max(captured[-1]["sample_times"]) <= report.wall[0]
    assert "latest 2s ago" in L.row_text(rows[-1])
    assert "ahead of clock" not in L.row_text(rows[-1])
    assert "999" not in L.row_text(rows[0])


@pytest.mark.parametrize("kind", ["unknown", "outage", "log-invalid"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_buffered_intersections_preserve_real_missing_values_and_outages(report, kind, ascii_):
    assert M.set_enabled(report.app, report.key, True)
    assert M.set_delta(report.app, report.key, 1.)
    if kind == "outage":
        report.points = [{"t": float(t), "value": 1.} for t in (180, 181, 182, 195, 196, 197)]
        report.wall[0] = 200.
    else:
        report.points[-2]["value"] = None if kind == "unknown" else 0.
        if kind == "log-invalid":
            report.app.analysis_state["axes"]["loss"] = {"mode": "log"}
    rows, metadata = draw(report, ascii_=ascii_)
    assert not metadata["has_data"]
    assert "between acquired samples" not in L.row_text(rows[0])


def test_visible_large_extrema_remain_in_scale_when_every_coarse_bucket_has_a_gap(report):
    report.points[2]["value"] = None
    report.points[6]["value"] = None
    rows, metadata = draw(report)
    assert metadata["has_data"]
    assert metadata["y_bounds"][1] >= 1190.
    assert "gaps 2" in L.row_text(rows[-1])


def test_gap_elsewhere_does_not_hide_large_measured_right_edge_intersection(report):
    report.points[2]["value"] = None
    report.points[6]["value"] = None
    report.points[-2]["value"] = 5000.
    _, metadata = draw(report)
    # The measured 190..195 segment intersects display end 192 at 2714.
    assert metadata["x_bounds"] == (150., 192.)
    assert metadata["y_bounds"][1] >= 2714.


def test_input_reuses_painted_playhead_without_advancing_or_rescanning_source(report, monkeypatch):
    app = report.app
    app.mode = "analysis"
    app.analysis_state.update(modal="chart", metric="loss")
    app.analysis_state["chart_frame"] = {"source": "/run/metrics.jsonl", "record": app.store.jobs[0]}
    draw(report, interactive=True)
    painted = app.analysis_state["chart_visible"]["points"]
    assert [point["t"] for point in painted][-1] == 190.
    report.wall[0] += 20
    monkeypatch.setattr(M, "display_end", lambda *a, **k: pytest.fail("Input advanced buffered playback"))
    monkeypatch.setattr(M, "window", lambda *a, **k: pytest.fail("Input recalculated the painted window"))
    monkeypatch.setattr(A, "_points", lambda *a, **k: pytest.fail("Input renormalized a painted source"))
    assert A._chart_visible_points(app, {"loss": report.points}, "loss") is painted


def test_explicit_modal_pan_and_zoom_retain_the_users_selected_time_domain(report):
    app = report.app
    app.analysis_state.update(zoom=2., pan=.5)
    rows, metadata = draw(report, interactive=True)
    assert metadata["x_bounds"] == (162.5, 187.5)
    report.wall[0] += 1
    assert draw(report, interactive=True)[1]["x_bounds"] == metadata["x_bounds"]
    assert "buffered" not in L.row_text(rows[-1]).lower()


def test_box_capture_keeps_source_coordinates_stable_as_playback_advances(report, monkeypatch):
    bounds = {"x": (180., 190.), "y": (1100., 1200.)}
    monkeypatch.setattr(C, "captured_bounds", lambda *a, **k: bounds)
    first = draw(report)[1]
    report.wall[0] += 1
    second = draw(report)[1]
    assert first["x_bounds"] == second["x_bounds"] == bounds["x"]
    assert first["y_bounds"] == second["y_bounds"] == bounds["y"]


def test_held_card_reuses_raster_but_updates_source_age_and_buffer_label(report, monkeypatch):
    report.wall[0] = 300.
    calls = []
    original = A.charts.braille_chart
    monkeypatch.setattr(A.charts, "braille_chart", lambda *a, **k: (calls.append(1), original(*a, **k))[1])
    first, before = draw(report)
    report.wall[0] += 60
    second, after = draw(report)
    assert len(calls) == 1
    assert before["x_bounds"] == after["x_bounds"]
    assert first[:-1] == second[:-1] and first[-1] != second[-1]
    assert "held" in L.row_text(second[-1]).lower()


def test_future_only_source_stays_empty_until_real_observations_arrive(report):
    report.points = [{"t": 210., "value": 50.}, {"t": 215., "value": 60.}]
    rows, metadata = draw(report)
    assert not metadata["has_data"]
    assert "0/2 samples" in L.row_text(rows[-1])
    assert "buffering" in L.row_text(rows[-1]).lower()


def test_completion_restores_full_retained_history_and_stops_buffering(report):
    assert draw(report)[1]["x_bounds"] == (150., 192.)
    report.app.store.jobs[0].state = "COMPLETED"
    M.set_running(report.app, report.key, False)
    rows, metadata = draw(report, running=False)
    assert metadata["x_bounds"] == (150., 200.)
    assert "11/11 samples" in L.row_text(rows[-1])
    assert "buffered" not in L.row_text(rows[-1]).lower()


def test_late_future_admission_invalidates_held_cache_without_changing_input_identity(report, monkeypatch):
    report.points = [{"t": 195., "value": 1195.}, {"t": 200., "value": 1200.}, {"t": 205., "value": 1205.}]
    report.wall[0] = 204.
    rows, first = draw(report)
    assert "latest 4s ago" in L.row_text(rows[-1])
    report.wall[0] = 205.
    rows, second = draw(report)
    assert "latest 0s ago" in L.row_text(rows[-1])
    assert first["key"] == second["key"] == report.key

