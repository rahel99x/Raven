"""Chart controls expose original observations and bounded, honest summaries."""
from types import SimpleNamespace
import copy
import math

import pytest

from raven import analysis_ui as A, chart_tools as C, charts, layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.research import ResearchHub


@pytest.fixture
def app():
    cfg = Config()
    store = Store(persist=False)
    store.jobs = [Job("9", "active", "main", "RUNNING", cpus=2)]
    store.finished = [Finished("10", "previous", "COMPLETED", cpus=2)]
    application = App(store, None, None, cfg, "observer")
    application.selected_id = "9"
    application.research = ResearchHub(cfg)
    application.analysis_result = {"path": "/reported/metrics.jsonl", "series": {
        "loss": [{"t": i * 100, "value": value, "step": i} for i, value in enumerate([4., 3., None, 1., 0., -1.])],
        "throughput": [{"t": i * 100, "value": value} for i, value in enumerate([1., 2., 3., 4., 5., 6.])]}}
    application.analysis_result_job = "9"
    application.analysis_result_generation = application.research.generation
    yield application
    application.research.close()


def command(app, *arguments):
    app.command_ok = True
    assert A.run_command(app, list(arguments))
    return app.command_ok


def overlay(app, ascii_=False, width=120, height=35):
    positioned = A.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), app.store.snapshot(), app, width, height)
    return "\n".join(L.row_text(row) for _, _, row in positioned)


@pytest.mark.parametrize("preset,expected", [("5m", 300), ("30m", 1800), ("2h", 7200), ("all", None)])
def test_presets_select_retained_end_and_display_actual_window(app, preset, expected):
    command(app, "chart", "loss")
    command(app, "chart", "preset", preset)
    visible, times = A.viewport(app.analysis_result["series"]["loss"], app.analysis_state)
    assert times[-1] == 500 and visible[-1]["t"] == 500
    assert app.analysis_state.get("window") == expected
    assert f"[{preset}]" in overlay(app)
    if expected == 300:
        assert times == (200, 500) and visible[0]["value"] is None


def test_preset_keyboard_cycle_and_custom_window_use_same_viewport(app):
    command(app, "chart", "loss")
    A.handle_key(app, "t")
    assert app.analysis_state["window"] == 300
    command(app, "chart", "window", "100")
    assert A.viewport(app.analysis_result["series"]["loss"], app.analysis_state)[1] == (400, 500)
    assert "[custom]" in overlay(app) and "[5m]" not in overlay(app)


@pytest.mark.parametrize("arguments", [("axis", "fixed", "1", "1"), ("axis", "fixed", "inf", "5"),
    ("axis", "log", "0", "10"), ("axis", "log", "-1", "5"), ("axis", "auto", "1", "5"),
    ("preset", "9m"), ("range", "0", "3"), ("range", "1", "999"), ("shared", "maybe")])
def test_invalid_controls_leave_preferences_and_range_intact(app, arguments):
    command(app, "chart", "loss")
    before = copy.deepcopy(A.save(app))
    assert not command(app, "chart", *arguments)
    assert A.save(app) == before and "chart_range" not in app.analysis_state


@pytest.mark.parametrize("ascii_", [False, True])
def test_log_axis_retains_exact_zero_negative_and_unknown_values(app, ascii_):
    command(app, "chart", "loss")
    command(app, "chart", "axis", "log")
    assert "2 nonpositive samples undefined" in overlay(app, ascii_)
    A.handle_key(app, "end")
    assert "value -1.0" in overlay(app, ascii_)
    plotted, low, high, count = C.axis_values([0., -1., None, 1., 100.], {"mode": "log"})
    assert plotted == [None, None, None, 0., 2.] and count == 2 and low == 0 and high > 2
    A.handle_key(app, "a")
    assert app.analysis_state["axes"]["loss"] == {"mode": "auto"}


def test_fixed_scale_uses_requested_bounds_and_reports_clipped_values(app, monkeypatch):
    command(app, "chart", "loss")
    command(app, "chart", "axis", "fixed", "1", "3")
    calls = []
    original = charts.braille_chart
    def capture(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(charts, "braille_chart", capture)
    assert "3 outside bounds" in overlay(app)
    assert calls[0]["lo"] == 1 and calls[0]["hi"] == 3


def test_interval_keyboard_selects_source_times_and_reports_statistics(app):
    command(app, "chart", "throughput")
    A.handle_key(app, "r")
    A.handle_key(app, "right")
    A.handle_key(app, "right")
    assert app.analysis_state["chart_range"] == {"metric": "throughput", "start": 0, "end": 200, "selecting": True}
    A.handle_key(app, "r")
    rendered = overlay(app)
    assert "3/3 known samples" in rendered and "median 2" in rendered and "Coverage: samples 100.0%" in rendered
    assert "P05" in rendered and "P95" in rendered and "P99" in rendered
    rows = A.chart_rows(L.Glyphs(False), app, app.analysis_result["series"]["throughput"], 100, 5, "throughput", "reported")
    assert any("+rev" in style for row in rows[1:6] for _, style in row)
    command(app, "chart", "range", "2", "5")
    assert app.analysis_state["chart_range"]["start"] == 100 and app.analysis_state["chart_range"]["end"] == 400
    command(app, "chart", "range", "clear")
    assert "chart_range" not in app.analysis_state


def test_interval_percentiles_and_outage_coverage_do_not_impute_missing_readings():
    points = [{"t": t, "value": value} for t, value in [(0, 0), (1, 10), (2, None), (100, 20), (101, 30)]]
    summary = C.statistics_for(points)
    assert summary["count"] == 4 and summary["missing"] == 1
    assert summary["minimum"] == 0 and summary["maximum"] == 30 and summary["mean"] == 15
    assert summary["median"] == 15 and summary["p95"] == pytest.approx(28.5)
    assert summary["sample_coverage"] == .8
    assert summary["time_coverage"] == pytest.approx(2 / 101)
    assert C.statistics_for(points[:1])["time_coverage"] is None
    assert C.statistics_for([{"t": 1, "value": None}])["mean"] is None


def test_statistics_remain_finite_for_extreme_values():
    points = [{"t": i, "value": value} for i, value in enumerate([-1e308, 1e308, 1e308])]
    summary = C.statistics_for(points)
    assert summary["mean"] == pytest.approx(1e308 / 3)
    assert all(math.isfinite(summary[key]) for key in ("mean", "median", "p05", "p95", "p99"))


@pytest.mark.parametrize("spike,baseline,target_row", [(100, 0, 0), (0, 100, -1)])
def test_compressed_unicode_chart_renders_both_extremes(spike, baseline, target_row):
    values = [baseline] * 1000
    values[500] = spike
    rows = charts.braille_chart(L.Glyphs(False), values, 18, 5, lo=0, hi=100)
    plots = rows[:-1]
    assert any(char in charts.BRAILLE[1:] for char in L.row_text(plots[target_row])[10:])
    buckets, _ = charts._time_points(values, range(1000), 8, None, 1, envelope=True)
    assert any(value == spike for _, value, _ in buckets) and len(buckets) <= 32


def test_ascii_envelope_exposes_minimum_and_maximum_inside_same_column():
    values = [0] * 1000
    values[500] = 100
    rows = charts.braille_chart(L.Glyphs(True), values, 18, 5, lo=0, hi=100)
    assert ":" in L.row_text(rows[0])[10:] and ":" in L.row_text(rows[4])[10:]
    assert all(L.row_text(row).isascii() for row in rows)


def test_envelope_keeps_unknown_buckets_and_detected_outages_as_gaps():
    points, _ = charts._time_points([0, 100, None, 0, 100], [0, .1, .2, 100, 100.1], 2, None, 1, envelope=True)
    assert points[0][1] is None and not points[-2][2]
    assert charts.envelope_points([1, None, 100, 200], 2)[0] == (0, None, False)
    collapsed, _ = charts._time_points([0, 100], [0, 1000], 1, None, 1, envelope=True)
    assert [value for _, value, _ in collapsed] == [0, 100] and not collapsed[-1][2]


def test_shared_scale_toggle_changes_rendered_comparison_bounds(app, monkeypatch):
    app.store.series["9"].extend([{"t": 1, "k": "live", "cpu": .1}, {"t": 2, "k": "live", "cpu": .2}])
    app.store.series["10"].extend([{"t": 1, "k": "live", "cpu": .8}, {"t": 2, "k": "live", "cpu": .9}])
    command(app, "diff", "9", "10")
    calls = []
    original = charts.braille_chart
    def capture(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(charts, "braille_chart", capture)
    rendered = "\n".join(L.row_text(row) for row in A._job_diff_rows(L.Glyphs(False), app.store.snapshot(), app, 100))
    assert [(call["lo"], call["hi"]) for call in calls[:2]] == [(0., 90.), (0., 90.)]
    assert "90%" in rendered and "21%" not in rendered
    calls.clear()
    A.handle_key(app, "s")
    rendered = "\n".join(L.row_text(row) for row in A._job_diff_rows(L.Glyphs(False), app.store.snapshot(), app, 100))
    assert calls[0]["hi"] is None and calls[1]["hi"] is None
    assert "21%" in rendered and "94%" in rendered
    command(app, "chart", "shared", "on")
    assert app.mode == "analysis" and app.analysis_state["modal"] == "diff" and app.analysis_state["shared_scale"]


def test_display_preferences_persist_without_rewriting_samples_or_ids(app):
    before = copy.deepcopy(app.analysis_result)
    command(app, "metricdisplay", "loss", "label", "Validation", "loss")
    command(app, "metricdisplay", "loss", "precision", "4")
    command(app, "metricdisplay", "loss", "unit", "score")
    command(app, "chart", "loss")
    rendered = overlay(app)
    assert "Validation loss" in rendered and "4.0000 score" in rendered
    assert "ID loss" in rendered and "declared unit score" in rendered and "value 4.0" in rendered
    assert app.analysis_result == before
    clone = SimpleNamespace()
    A.restore(clone, A.save(app))
    assert clone.analysis_state["metric_display"] == app.analysis_state["metric_display"]
    assert clone.analysis_state["modal"] == "" and "chart_job" not in clone.analysis_state
    command(app, "metricdisplay", "loss", "reset")
    assert "loss" not in app.analysis_state["metric_display"]


def test_untrusted_display_preferences_are_bounded_and_drop_unknown_axis_fields():
    saved = {"metric_display": {**{f"m{i}": {"label": f"Metric {i}"} for i in range(100)},
                                "bad": {"unit": "s\x1b"}},
             "axes": {"valid": {"mode": "fixed", "low": -1, "high": 1, "large": [1] * 1000},
                      "invalid": {"mode": "log", "low": 0, "high": 1}}}
    restored = C.restore(saved)
    assert len(restored["metric_display"]) == 64
    assert restored["axes"] == {"valid": {"mode": "fixed", "low": -1, "high": 1}}


def test_chart_events_pin_job_and_open_original_citation_with_return_location(app, monkeypatch):
    command(app, "chart", "loss")
    app.store.events.extend([
        {"t": 100, "kind": "checkpoint", "job": "9", "text": "Saved checkpoint", "path": "/actual/worker.err", "line": 52},
        {"t": 200, "kind": "alert", "job": "10", "text": "Different job"},
        {"t": 300, "kind": "alert", "job": "9", "text": "Reported alert"}])
    app.selected_id = "10"
    rendered = overlay(app)
    assert "Events e" in rendered
    A.handle_key(app, "right")
    A.handle_key(app, "e")
    rendered = overlay(app)
    assert "Saved checkpoint" in rendered and "/actual/worker.err" in rendered and "original line 52" in rendered
    assert "Different job" not in rendered and len(A.chart_events(app, app.store.snapshot())) == 2
    seen = []
    from raven import log_workbench
    monkeypatch.setattr(log_workbench, "open_citation", lambda application, citation: seen.append(citation) or True)
    A.handle_key(app, "enter")
    assert seen[0]["path"] == "/actual/worker.err" and seen[0]["line"] == 52 and seen[0]["job"] == "9"
    captured = app.navigation_state["stack"][-1]
    assert captured["analysis_context"]["modal"] == "chart_events"


def test_event_picker_escape_restores_sample_cursor_and_event_toggle_is_reversible(app):
    command(app, "chart", "loss")
    A.handle_key(app, "right")
    A.handle_key(app, "e")
    A.handle_key(app, "esc")
    assert app.analysis_state["modal"] == "chart" and app.analysis_state["cursor"] == 1
    command(app, "chart", "events", "off")
    assert not app.analysis_state["chart_events"]
    command(app, "chart", "events", "on")
    assert app.analysis_state["chart_events"]


def test_chart_return_from_inspector_retains_range_and_works_without_optional_fields(app):
    command(app, "chart", "loss")
    app.analysis_state.pop("preset", None)
    assert A.open_inspector(app, "10")
    A.handle_key(app, "esc")
    assert app.analysis_state["modal"] == "chart" and "chart_range" not in app.analysis_state
    A.handle_key(app, "t")
    assert app.analysis_state["window"] == 300
    command(app, "chart", "range", "1", "2")
    expected = dict(app.analysis_state["chart_range"])
    assert A.open_inspector(app, "10")
    A.handle_key(app, "esc")
    assert app.analysis_state["chart_range"] == expected


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("size", [(1, 1), (30, 10), (80, 24), (160, 50)])
def test_new_chart_views_survive_resize_and_never_draw_outside_terminal(app, ascii_, size):
    command(app, "chart", "loss")
    command(app, "chart", "range", "1", "6")
    command(app, "chart", "axis", "log")
    for modal in ("chart", "chart_events"):
        app.analysis_state["modal"] = modal
        rows = A.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), app.store.snapshot(), app, *size)
        assert all(0 <= y < size[1] and 0 <= x < size[0] and x + L.vlen(L.row_text(row)) <= size[0] for y, x, row in rows)
        assert all(all(char.isprintable() for char in L.row_text(row)) for _, _, row in rows)
        if ascii_:
            assert all(L.row_text(row).isascii() for _, _, row in rows)
