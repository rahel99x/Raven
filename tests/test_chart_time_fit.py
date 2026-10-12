"""Time selection fits evidence and keeps thin selectors on the actual canvas."""
from copy import deepcopy
from dataclasses import replace
import math
import sys
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, charts, layout as L, palette as P
from raven.config import Config
from raven.views import Views


@pytest.fixture
def app():
    value = SimpleNamespace(mode="main", tab="analytics", width=120, height=40,
                            selected_id="101", analytics_job="101", analytics_view="job",
                            toolbar_state={}, project_state={}, job_panel_state={},
                            research=SimpleNamespace(generation=3), theme="dark", messages=[])
    value.say = value.messages.append
    A.initialize(value)
    C.begin_frame(value, 120, 40)
    return value


def plot(app, identity=("job", "101", "CPU")):
    C.record(app, identity, {"plot_rect": (2, 10, 12, 60), "x_bounds": (0., 100.),
                             "y_bounds": (0., 200.)})
    return C.publish(app, 120, 40)[-1]


@pytest.mark.parametrize("start,end", [((3, 15), (3, 45)), ((3, 45), (3, 15)),
                                     ((3, 15), (9, 45)), ((9, 45), (3, 15))])
def test_unmodified_drag_selects_a_time_interval_even_on_one_row(app, start, end):
    item = plot(app)
    C.handle_mouse(app, *start, button="press")
    C.handle_mouse(app, *end, button="release")
    assert C.autofit(app, item.key)
    assert C.bounds(app, item.key)["x"] == pytest.approx((100 * 5 / 49, 100 * 35 / 49))
    assert "Y fits observed data" in app.messages[-1]


def test_shift_horizontal_drag_cancels_but_shift_area_preserves_explicit_y(app):
    item = plot(app)
    C.handle_mouse(app, 3, 15, button="press", shift=True)
    C.handle_mouse(app, 3, 45, button="release")
    assert C.bounds(app, item.key) is None
    C.handle_mouse(app, 3, 15, button="press", shift=True)
    C.handle_mouse(app, 9, 45, button="release")
    assert not C.autofit(app, item.key)
    assert C.bounds(app, item.key)["y"] == pytest.approx((200 * 2 / 9, 200 * 8 / 9))


def test_undo_restores_fit_intent_and_scale_guard_keeps_other_sources_independent(app):
    item = plot(app)
    C._apply(app, item, {"x": (1., 20.), "y": (0., 200.), "fit_y": True})
    C._apply(app, item, {"x": (2., 15.), "y": (40., 80.), "fit_y": False})
    assert not C.autofit(app, item.key)
    assert C.undo(app, item.key) and C.autofit(app, item.key)
    assert not C.autofit(app, item.key, scale="log")
    assert not C.autofit(app, ("another",))
    assert C.reset(app, item.key) and not C.autofit(app, item.key)


@pytest.mark.parametrize("reverse", [False, True])
def test_fit_contains_true_boundary_intersections_without_whole_trace_extrema(reverse):
    values = [0., 100.] if not reverse else [100., 0.]
    expected = deepcopy(values)
    lo, hi = charts.fit_time_bounds(values, [0., 100.], (40., 60.), (0., 100.), 100.)
    assert (lo, hi) == pytest.approx((39.5, 60.5))
    assert values == expected


def test_fit_retains_narrow_spikes_and_duplicate_time_extrema():
    values = [1.] * 1000
    values[500] = 900.
    lo, hi = charts.fit_time_bounds(values, list(range(1000)), (499., 502.), (0., 1000.), 1.)
    assert lo <= 1. and hi >= 900. and hi < 1000.
    lo, hi = charts.fit_time_bounds([2., 999., 3.], [10., 10., 11.], (9., 12.), (0., 1000.), 1.)
    assert lo <= 2. and hi >= 999.


@pytest.mark.parametrize("values,interval", [([None, 100.], 100.), ([0., None], 100.),
                                            ([math.nan, 100.], 100.), ([0., math.inf], 100.),
                                            ([0., 100.], 1.), ([True, 100.], 100.)])
def test_empty_missing_and_outage_intervals_retain_fallback_without_fake_interpolation(values, interval):
    assert charts.fit_time_bounds(values, [0., 100.], (40., 60.), (-5., 105.), interval) == (-5., 105.)


@pytest.mark.parametrize("value", [0., 40., -40., 1e-310, math.ulp(0.), sys.float_info.max,
                                 -sys.float_info.max])
def test_flat_extreme_and_subnormal_fits_are_finite_and_enclose_observations(value):
    lo, hi = charts.fit_time_bounds([value, value], [0., 1.], (0., 1.), (-1., 1.), 1.)
    assert math.isfinite(lo) and math.isfinite(hi) and lo < hi
    assert lo <= value <= hi


@pytest.mark.parametrize("times", [(0., 0.), (2., 1.), (math.nan, 1.), (0., math.inf), None])
def test_bad_selected_ranges_do_not_override_safe_axes(times):
    assert charts.fit_time_bounds([1., 2.], [0., 1.], times, (0., 3.), 1.) == (0., 3.)


@pytest.mark.parametrize("unit,span", [("s", 5.), ("ms", .01), ("us", .00001)])
@pytest.mark.parametrize("epoch", [0., 1700000000., -1700000000.])
@pytest.mark.parametrize("width", [30, 80, 120])
def test_selected_time_axis_uses_distinct_collision_free_relative_ticks(epoch, span, unit, width):
    text = L.row_text(charts.time_axis(epoch, epoch + span, width, units=True))
    labels = text.split()
    assert L.vlen(text) == width
    assert labels[0] == "0" + unit and len(labels) >= 2
    assert all(label.endswith(unit) for label in labels)
    assert len(set(labels)) == len(labels)
    assert "nan" not in text and "inf" not in text
    assert L.row_text(charts.time_selection_note(epoch, epoch + span, 120)).startswith(
        f"   Time +offset from t_a={epoch!r}s;")


@pytest.mark.parametrize("epoch,span", [(1700000000., .000001), (1e300, math.ulp(1e300) * 3),
                                      (0., math.ulp(0.) * 5), (-1e308, 1e308)])
def test_offset_axis_respects_finite_timestamp_resolution_and_never_emits_invalid_numbers(epoch, span):
    text = L.row_text(charts.time_axis(epoch, epoch + span, 120, units=True))
    assert "nan" not in text.lower() and "inf" not in text.lower()
    assert L.vlen(text) == 120 and len(text.split()) == len(set(text.split()))


@pytest.mark.parametrize("theme", P.THEME_NAMES)
@pytest.mark.parametrize("source_style", ["dim", "chart-1+bg:surface", "chart-2+bg:surface-sunken",
                                         "green+bg:#fb7185", "chart-3+rev", "sel+bg:surface-raised"])
def test_selector_inherits_resolved_plot_background_in_all_themes(app, theme, source_style):
    app.theme = theme
    item = plot(app)
    rows = [[(" " * 120, source_style)] for _ in range(40)]
    C.hover(app, 5, 30)
    selector = C.feedback(app, rows=rows)
    source = P.resolve(P.cell_style(source_style, theme), theme)
    expected = source.foreground if "rev" in source.flags else source.background
    for y, x, row in selector:
        assert item.visible.contains(y, x)
        style = P.resolve(P.cell_style(row[0][1], theme), theme)
        assert style.background == expected
        assert style.foreground == P.resolve("accent", theme).foreground
        assert "bold" not in row[0][1] and "rev" not in row[0][1]


def test_topmost_nested_overlay_retains_ink_and_wide_cells_are_not_split(app):
    plot(app)
    C.hover(app, 5, 30)
    rows = [[(" " * 120, "bg:surface")]] * 40
    overlays = [(5, 20, [("界" * 10, "bg:surface-sunken")]),
                (5, 29, [("XX", "chart-3+bg:surface-raised")])]
    feedback = {(y, x): row for y, x, row in C.feedback(app, rows=rows, overlays=overlays)}
    expected = P.resolve(P.cell_style("bg:surface-raised", app.theme), app.theme).background
    assert P.resolve(feedback[(5, 30)][0][1], app.theme).background == expected
    assert feedback[(5, 30)] == [("X", "chart-3+bg:surface-raised")]
    assert (5, 25) not in feedback


@pytest.mark.parametrize("theme", P.THEME_NAMES + ("monokai", "gruvbox", "Gruvbox Dark", "Modnokai"))
@pytest.mark.parametrize("dragging", [False, True])
def test_all_selector_marks_use_the_current_theme_accent_and_retain_their_background(app, theme, dragging):
    app.theme = theme
    app.animations_enabled = False
    plot(app)
    rows = [[(" " * 120, "bg:surface-sunken")]] * 40
    C.hover(app, 5, 30)
    if dragging:
        C.handle_mouse(app, 4, 20, button="press")
        C.handle_mouse(app, 9, 45, button="drag")
    feedback = C.feedback(app, rows=rows)
    assert {char for _, _, row in feedback for char, _ in row} == (
        {".", "+"} if theme == "reader" else {"⠤", "⢸", "⢼"})
    accent = P.resolve("accent", theme).foreground
    background = P.resolve(P.cell_style("bg:surface-sunken", theme), theme).background
    for _, _, row in feedback:
        for char, style in row:
            resolved = P.resolve(P.cell_style(style, theme), theme)
            assert style.split("+")[0] == "accent"
            assert "bold" not in style.split("+")
            assert resolved.foreground == accent and resolved.background == background
    if P.canonical_theme(theme) in ("darcula", "gruvbox-dark"):
        assert accent == P.resolve("cyan", theme).foreground
        assert accent == P.resolve("cursor", theme).foreground


@pytest.mark.parametrize("before,after", [("darcula", "gruvbox-dark"), ("gruvbox", "light"),
                                         ("monokai", "darcula"), ("dark", "modnokai")])
@pytest.mark.parametrize("ascii_", [False, True])
def test_republished_graph_selector_changes_its_accent_and_background_on_theme_switch(app, before, after, ascii_):
    app.theme = before
    item = plot(app)
    rows = [[(" " * 120, "bg:surface")]] * 40
    C.hover(app, 5, 30)
    original = C.feedback(app, rows=rows, ascii_=ascii_)
    app.theme = after
    C.begin_frame(app, 120, 40)
    current = plot(app)
    changed = C.feedback(app, rows=rows, ascii_=ascii_)
    assert current.rect == item.rect and current.key == item.key
    assert len(changed) == len(original)
    assert {char for _, _, row in changed for char, _ in row} == ({".", "+"} if ascii_ else {"⠤", "⢸", "⢼"})
    old_style = P.resolve(P.cell_style(original[0][2][0][1], before), before)
    new_style = P.resolve(P.cell_style(changed[0][2][0][1], after), after)
    assert old_style.foreground != new_style.foreground
    assert new_style.foreground == P.resolve("accent", after).foreground
    assert new_style.background == P.resolve(P.cell_style("bg:surface", after), after).background
    assert new_style.background != old_style.background


@pytest.mark.parametrize("value", ["#", "#12345", "#GGGGGG", "surface", "#1234567", "\x1b[31m"])
def test_malformed_raw_background_does_not_override_prior_safe_style(value):
    parsed = P.resolve("cyan+bg:surface+bg-raw:" + value, "darcula")
    expected = P.resolve("cyan+bg:surface", "darcula")
    assert parsed == expected


@pytest.mark.parametrize("ascii_", [False, True])
def test_native_shared_renderer_fits_selected_series_and_restores_percent_origin_on_reset(app, ascii_):
    views = Views(L.Glyphs(ascii_), Config({}))
    identity = ("resource-series", "101", "cpu", "%")
    item = plot(app, identity)
    C._apply(app, item, {"x": (40., 60.), "y": (80., 90.), "fit_y": True})
    C.begin_frame(app, 120, 40)
    rows = views.metric_curve(app, [0., 100.], 100, 8, identity, lo=0, hi=100, unit="%",
                             sample_times=[0., 100.], sample_interval=100., title="CPU")
    painted = C.publish(app, 120, 40)[0]
    assert painted.y_bounds == pytest.approx((39.5, 60.5))
    text = "\n".join(L.row_text(row) for row in rows)
    assert "0s" in text and "20s" in text and "t_a=40.0s" in text
    assert "39.5%" in text and "60.5%" in text and "awaiting samples" in text
    assert C.reset(app, identity)
    C.begin_frame(app, 120, 40)
    views.metric_curve(app, [0., 100.], 100, 8, identity, lo=0, hi=100, unit="%",
                       sample_times=[0., 100.], sample_interval=100., title="CPU")
    assert C.publish(app, 120, 40)[0].y_bounds == (0., 100.)


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("interactive", [False, True])
def test_reported_chart_fit_and_cached_geometry_preserve_original_statistics(app, ascii_, interactive):
    points = [{"t": 1700000000. + i * .00001, "value": 50. + i * .00001} for i in range(11)]
    original = deepcopy(points)
    identity = A.chart_key(app, "CPU per core (%)", "reported", interactive=interactive)
    selected = (points[2]["t"], points[8]["t"])
    C._apply(app, replace(plot(app), key=identity), {"x": selected, "y": (0., 100.), "fit_y": True})
    metadata = {}
    rows = A.chart_rows(L.Glyphs(ascii_), app, points, 100, 8, "CPU per core (%)", "reported",
                        interactive=interactive, metadata=metadata)
    lo, hi = metadata["y_bounds"]
    assert 49.99 < lo <= points[2]["value"] <= points[8]["value"] <= hi < 50.01
    assert metadata["x_bounds"] == selected
    assert metadata["plot_rect"][1] > 10  # precise Y labels widen the gutter
    text = "\n".join(L.row_text(row) for row in rows)
    assert "us" in text and "t_a=1700000000.00002s" in text
    assert points == original
    if not interactive:
        repeated = {}
        assert A.chart_rows(L.Glyphs(ascii_), app, points, 100, 8, "CPU per core (%)", "reported",
                            interactive=False, metadata=repeated) == rows
        assert repeated == metadata


def test_fitted_gutter_is_respected_by_sample_crosshair_and_range_highlight(app):
    rows = [[(" " * 80, "")]] * 8
    points = [{"t": 0., "value": 1.}, {"t": 10., "value": 2.}]
    rect = (1, 16, 6, 80)
    cursor = A._crosshair(rows, 80, 5, points, points[0], (0., 10.), False, rect)
    assert L.row_text(cursor[1])[16] == "│" and L.row_text(cursor[1])[10] == " "
    highlighted = A._highlight_interval(rows, 80, 5, points, (0., 10.), False, rect)
    assert all("rev" not in style for text, style in highlighted[1][:16])
    assert all("rev" in style for text, style in highlighted[1][16:])
