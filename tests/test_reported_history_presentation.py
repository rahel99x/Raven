"""Full-history publication gates preserve measured edges and exact plot maps."""
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, clock, layout as L, metric_live as M
from raven.model import Job


@pytest.fixture
def report(monkeypatch):
    now = [5001.]
    monkeypatch.setattr(clock, "now", lambda: now[0])
    app = SimpleNamespace(selected_id="1", research_job_id="1", tab="research", mode="main",
                          width=100, height=40, cfg={}, project_state={},
                          research=SimpleNamespace(generation=0), toolbar_state={}, job_panel_state={},
                          store=SimpleNamespace(jobs=[Job("1", "train", "gpu", "RUNNING")]))
    A.initialize(app).update(chart_events=False, chart_job="1")
    identity = A.chart_key(app, "loss", "reported.jsonl", interactive=False)
    points = [{"t": float(t), "value": 40.} for t in range(0, 5001, 5)]
    calls = []
    painter = A.charts.braille_chart
    monkeypatch.setattr(A.charts, "braille_chart", lambda *a, **k: (calls.append(k["times"]), painter(*a, **k))[1])
    return SimpleNamespace(app=app, now=now, identity=identity, points=points, calls=calls)


def draw(report, *, interactive=False, ascii_=False, width=100):
    metadata = {}
    rows = A.chart_rows(L.Glyphs(ascii_), report.app, report.points, width, 8, "loss", "reported.jsonl",
                        zoom_key=report.identity, metadata=metadata, running=True, interactive=interactive)
    return rows, metadata


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("ascii_", [False, True])
def test_unmodified_flat_history_reuses_raster_and_reports_its_displayed_lag(report, interactive, ascii_):
    first, metadata = draw(report, interactive=interactive, ascii_=ascii_)
    assert metadata["x_bounds"] == (0., 4991.)
    for _ in range(4):
        report.now[0] += .2
        rows, current = draw(report, interactive=interactive, ascii_=ascii_)
        assert current["x_bounds"] == metadata["x_bounds"]
        assert rows[:-1] == first[:-1]
    assert len(report.calls) == 1
    assert "10.8s behind" in L.row_text(rows[-1])
    if interactive:
        assert report.app.analysis_state["chart_display_window"] == metadata["x_bounds"]
    else:
        assert len(report.app.analysis_state["chart_card_cache"]) == 1


@pytest.mark.parametrize("kind", ["flat", "spike", "missing"])
def test_crossing_an_acquired_vertex_always_publishes_even_without_x_pixel_motion(report, kind):
    if kind == "spike":
        report.points[-2]["value"] = 1000.
    elif kind == "missing":
        report.points[-2]["value"] = None
    draw(report)
    report.now[0] = 5005.1
    _, metadata = draw(report)
    assert metadata["x_bounds"][1] == pytest.approx(4995.1)
    assert len(report.calls) == 2


@pytest.mark.parametrize("axis", ["fixed", "log"])
def test_steep_edge_y_pixel_movement_bypasses_x_gate(report, axis):
    report.app.analysis_state["axes"]["loss"] = {"mode": axis, "low": 1., "high": 100.}
    report.points[-3]["value"], report.points[-2]["value"] = 1., 100.
    _, before = draw(report)
    report.now[0] += .5
    _, after = draw(report)
    assert after["x_bounds"][1] > before["x_bounds"][1]
    assert len(report.calls) == 2
    assert report.calls[-1] == after["x_bounds"]


def test_auto_y_growth_publishes_new_axis_instead_of_reusing_a_stale_scale(report):
    for point in report.points:
        point["value"] = 1.
    report.points[-2]["value"] = 1000.
    _, before = draw(report)
    report.now[0] += .01
    _, after = draw(report)
    assert after["x_bounds"][1] > before["x_bounds"][1]
    assert after["y_bounds"][1] > before["y_bounds"][1]
    assert len(report.calls) == 2


def test_inplace_interior_correction_forces_publication_with_unchanged_edge_and_scale(report):
    _, before = draw(report)
    report.now[0] += .2
    report.points[10]["value"] = 39.
    _, after = draw(report)
    assert after["x_bounds"][1] > before["x_bounds"][1]
    assert after["y_bounds"] == before["y_bounds"]
    assert len(report.calls) == 2


@pytest.mark.parametrize("interactive", [False, True])
def test_live_mode_bypasses_gate_and_returning_to_history_starts_from_current_clock(report, interactive):
    draw(report, interactive=interactive)
    report.now[0] += .2
    assert M.set_enabled(report.app, report.identity, True)
    assert M.set_delta(report.app, report.identity, 1.)
    _, first = draw(report, interactive=interactive)
    report.now[0] += .2
    _, second = draw(report, interactive=interactive)
    assert second["x_bounds"][1] > first["x_bounds"][1]
    assert second["x_bounds"][1] - second["x_bounds"][0] == 1.
    assert M.set_enabled(report.app, report.identity, False)
    _, returned = draw(report, interactive=interactive)
    assert returned["x_bounds"] == (0., second["x_bounds"][1])


def test_explicit_box_and_capture_keep_their_own_exact_time_domain(report, monkeypatch):
    draw(report)
    bounds = {"x": (100., 120.), "y": (0., 100.)}
    monkeypatch.setattr(C, "bounds", lambda *a, **k: bounds)
    monkeypatch.setattr(C, "captured_bounds", lambda *a, **k: bounds)
    for _ in range(3):
        report.now[0] += .2
        _, metadata = draw(report)
        assert metadata["x_bounds"] == bounds["x"] and metadata["y_bounds"] == bounds["y"]


def test_geometry_and_poll_changes_publish_immediately(report):
    _, before = draw(report)
    report.now[0] += .2
    _, resized = draw(report, width=120)
    assert resized["x_bounds"][1] > before["x_bounds"][1]
    report.now[0] += .2
    assert M.set_rate(report.app, report.identity, 100)
    _, changed = draw(report, width=120)
    assert changed["x_bounds"][1] > resized["x_bounds"][1]


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("changed", ["replay", "attempt", "control"])
def test_short_forward_replay_or_attempt_change_publishes_current_clock(report, interactive, changed):
    report.app.replay = SimpleNamespace(clock=SimpleNamespace(generation=0))
    _, before = draw(report, interactive=interactive)
    report.now[0] += .2
    entry = M.initialize(report.app)["entries"][M.canonical(report.identity)]
    if changed == "replay":
        report.app.replay.clock.generation += 1
    elif changed == "attempt":
        entry["generation"] = (id(report.app.store), 1)
    else:
        entry["token"] += "new"
    _, after = draw(report, interactive=interactive)
    assert after["x_bounds"][1] > before["x_bounds"][1]
    assert len(report.calls) == 2


@pytest.mark.parametrize("missing", ["candidate", "source_token"])
def test_unavailable_presentation_inputs_discard_old_gate(report, monkeypatch, missing):
    draw(report, interactive=True)
    from raven import metric_playback as P
    resets = []
    original = P.reset_presentation
    monkeypatch.setattr(P, "reset_presentation", lambda *args: (resets.append(args[-1]), original(*args))[1])
    if missing == "candidate":
        monkeypatch.setattr(M, "playback_status", lambda *args: None)
    report.now[0] += .2
    if missing == "source_token":
        prepared = A._prepared_card_source
        monkeypatch.setattr(A, "_prepared_card_source", lambda *args: dict(prepared(*args), token=None))
    draw(report, interactive=True)
    assert ("reported-modal", report.identity) in resets


def test_history_raster_cache_is_bounded_and_not_persisted(report):
    for width in range(90, 102):
        draw(report, width=width)
    assert len(report.app.analysis_state["chart_history_rasters"]) == A.MAX_CARD_CACHE
    saved = A.save(report.app)["analysis"]
    assert "chart_history_rasters" not in saved and "chart_source_sequence" not in saved
