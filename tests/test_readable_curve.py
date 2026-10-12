"""Connected curves retain evidence while remaining legible in small terminals."""
import copy
import math
import sys

import pytest

from raven import charts, layout as L, palette


def render(values, *, ascii_=False, width=50, height=6, **kwargs):
    metadata = {}
    rows = charts.braille_chart(L.Glyphs(ascii_), values, width, height,
                                metadata=metadata, **kwargs)
    return rows, metadata


def ink(rows, metadata, ascii_=False):
    top, left, bottom, right = metadata["plot_rect"]
    allowed = set("./\\-:|+") if ascii_ else set(charts.BRAILLE[1:])
    return [[char in allowed for char in L.row_text(row)[left:right]]
            for row in rows[top:bottom]]


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("value", [-200., 200.])
def test_points_wholly_outside_fixed_bounds_do_not_fabricate_boundary_plateaus(ascii_, value):
    rows, metadata = render([value] * 11, ascii_=ascii_, lo=0, hi=100,
                            sample_times=list(range(11)), sample_interval=1)
    assert not any(any(row) for row in ink(rows, metadata, ascii_))
    assert metadata["y_bounds"] == (0, 100)


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("values", [[-100., 200.], [200., -100.]])
def test_y_clipping_intersects_actual_observed_segment_at_correct_x(ascii_, values):
    rows, metadata = render(values, ascii_=ascii_, lo=0, hi=100,
                            sample_times=[0., 1.], sample_interval=1)
    measured = ink(rows, metadata, ascii_)
    width = len(measured[0])
    active = [x for x in range(width) if any(row[x] for row in measured)]
    assert active
    # These endpoints cross y=0 and y=100 at one-third and two-thirds of time.
    assert min(active) == pytest.approx((width - 1) / 3, abs=1)
    assert max(active) == pytest.approx((width - 1) * 2 / 3, abs=1)
    assert all(not any(row[:min(active)]) and not any(row[max(active) + 1:]) for row in measured)
    assert any(measured[0]) and any(measured[-1])


@pytest.mark.parametrize("ascii_", [False, True])
def test_no_crossing_is_drawn_across_outage_even_when_outside_points_straddle_bounds(ascii_):
    rows, metadata = render([-100., 200.], ascii_=ascii_, lo=0, hi=100,
                            sample_times=[0., 1000.], sample_interval=1)
    assert not any(any(row) for row in ink(rows, metadata, ascii_))


@pytest.mark.parametrize("ascii_", [False, True])
def test_regular_line_is_connected_and_is_not_a_filled_area(ascii_):
    rows, metadata = render([10., 90.], ascii_=ascii_, lo=0, hi=100,
                            sample_times=[0., 1.], sample_interval=1)
    measured = ink(rows, metadata, ascii_)
    assert all(any(row[x] for row in measured) for x in range(len(measured[0])))
    assert max(sum(row[x] for row in measured) for x in range(len(measured[0]))) <= 2
    if ascii_:
        assert "/" in "".join(L.row_text(row) for row in rows)
    else:
        assert all("#" not in L.row_text(row) for row in rows)


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("values", [[0., 0.], [100., 100.]])
def test_real_baseline_or_exact_upper_bound_remains_visible(ascii_, values):
    rows, metadata = render(values, ascii_=ascii_, lo=0, hi=100,
                            sample_times=[0., 1.], sample_interval=1)
    measured = ink(rows, metadata, ascii_)
    row = -1 if values[0] == 0 else 0
    assert all(measured[row])
    assert sum(sum(row) for row in measured) == len(measured[0])


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("missing", [None, math.nan, math.inf, -math.inf])
def test_unknown_samples_leave_middle_empty_and_do_not_reuse_previous_value(ascii_, missing):
    rows, metadata = render([10., missing, 90.], ascii_=ascii_, lo=0, hi=100,
                            sample_times=[0., 1., 2.], sample_interval=1)
    measured = ink(rows, metadata, ascii_)
    assert any(row[0] for row in measured) and any(row[-1] for row in measured)
    assert all(not any(row[1:-1]) for row in measured)


@pytest.mark.parametrize("ascii_", [False, True])
def test_compressed_extrema_and_original_header_survive_mixed_order_and_flat_values(ascii_):
    values = [50.] * 4000
    values[127], values[128], values[3999] = 0., 100., 25.
    rows, metadata = render(values, ascii_=ascii_, width=90, height=6, lo=0, hi=100, title="CPU", unit="%")
    measured = ink(rows, metadata, ascii_)
    assert any(measured[0]) and any(measured[-1])
    header = L.row_text(rows[0])
    assert "last 25%" in header and "max 100%" in header and "min 0%" in header


@pytest.mark.parametrize("ascii_", [False, True])
def test_duplicate_timestamps_keep_vertical_range_instead_of_averaging_away_spike(ascii_):
    rows, metadata = render([0., 100., 20.], ascii_=ascii_, width=18, height=6, lo=0, hi=100,
                            sample_times=[0., 0., 0.], sample_interval=1)
    measured = ink(rows, metadata, ascii_)
    assert all(row[-1] for row in measured)
    assert all(not any(row[:-1]) for row in measured)
    assert metadata["x_bounds"] == (0., 0.)


@pytest.mark.parametrize("ascii_", [False, True])
def test_invalid_timestamps_do_not_crash_or_displace_valid_observations(ascii_):
    values = [10., 999., 999., 999., 90.]
    rows, metadata = render(values, ascii_=ascii_, lo=0, hi=100,
                            sample_times=[0., None, math.nan, object(), 1.], sample_interval=1)
    measured = ink(rows, metadata, ascii_)
    assert metadata["x_bounds"] == (0., 1.)
    assert all(any(row[x] for row in measured) for x in range(len(measured[0])))


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("title", ["", "metric"])
@pytest.mark.parametrize("height", [0, 1, 6])
@pytest.mark.parametrize("width", [0, 3, 10, 80])
def test_plot_metadata_matches_actual_clipped_geometry_and_clears_stale_fields(ascii_, title, height, width):
    metadata = {"plot_rect": "stale", "x_bounds": "stale", "obsolete": True}
    rows = charts.braille_chart(L.Glyphs(ascii_), [10., 20.], width, height, title=title,
                               lo=0, hi=100, sample_times=[100., 200.], metadata=metadata)
    top, left, bottom, right = metadata["plot_rect"]
    assert top == int(bool(title)) and bottom - top == height
    assert left == min(width, 10) and right == width
    assert metadata["x_bounds"] == (100., 200.) and metadata["y_bounds"] == (0, 100)
    assert metadata["valid"] is bool(width > 10 and height)
    assert metadata["raster"] == ((1, 1) if ascii_ else (2, 4))
    assert "obsolete" not in metadata
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)


@pytest.mark.parametrize("ascii_", [False, True])
def test_explicit_time_window_is_retained_when_it_contains_no_source_samples(ascii_):
    rows, metadata = render([10., 20.], ascii_=ascii_, lo=0, hi=100,
                            sample_times=[100., 200.], times=(300., 400.), sample_interval=1)
    assert metadata["x_bounds"] == (300., 400.)
    assert metadata["has_data"] is False
    assert not any(any(row) for row in ink(rows, metadata, ascii_))


def test_timestamp_less_right_aligned_samples_do_not_publish_a_false_time_transform():
    rows, metadata = render([10., 20.], lo=0, hi=100, times=(100., 200.))
    assert metadata["x_bounds"] is None
    measured = ink(rows, metadata)
    assert all(not any(row[:-1]) for row in measured)
    assert any(row[-1] for row in measured)


@pytest.mark.parametrize("ascii_", [False, True])
def test_original_samples_timestamps_and_color_callback_are_not_mutated(ascii_):
    values, timestamps = [1., None, 5., 2.], [0., 1., 2., 3.]
    expected = copy.deepcopy((values, timestamps))
    colors = []
    rows, metadata = render(values, ascii_=ascii_, hi=10, sample_times=timestamps,
                            color=lambda fraction: colors.append(fraction) or "magenta")
    assert (values, timestamps) == expected
    assert all(0 <= value <= 1 for value in colors)
    assert any(style == "magenta+bold" for row in rows for _, style in row)


@pytest.mark.parametrize("ascii_", [False, True])
def test_curve_semantic_color_changes_with_theme_without_raster_rebuild(ascii_):
    rows, metadata = render([0., 100.], ascii_=ascii_, hi=100)
    curve_styles = {style for row in rows for text, style in row if style.endswith("+bold")}
    assert curve_styles == {"chart-1+bold"}
    style = next(iter(curve_styles))
    assert palette.resolve(style, "dark").foreground != palette.resolve(style, "light").foreground
    assert all(not style.startswith("fg:#") for style in curve_styles)


@pytest.mark.parametrize("reverse", [False, True])
def test_y_intersections_remain_finite_for_opposite_ieee_extremes(reverse):
    maximum = sys.float_info.max
    values = (maximum, -maximum) if reverse else (-maximum, maximum)
    segment = charts._clip_curve_segment(0, values[0], 100, values[1], -maximum / 2, maximum / 2)
    assert segment is not None and all(math.isfinite(value) for value in segment)
    assert segment[0] == pytest.approx(25.) and segment[2] == pytest.approx(75.)
    assert abs(segment[1]) == maximum / 2 and abs(segment[3]) == maximum / 2


@pytest.mark.parametrize("ascii_", [False, True])
def test_bounded_geometry_caps_raster_even_when_terminal_dimensions_are_extreme(ascii_):
    rows, metadata = render([], ascii_=ascii_, width=10**9, height=0, hi=100,
                            sample_times=[], times=(0., 1.))
    assert metadata["plot_rect"] == (0, 10, 0, 10 + charts.MAX_COLUMNS)
    assert not metadata["valid"] and len(rows) == 2
    assert all(L.vlen(L.row_text(row)) <= 10 + charts.MAX_COLUMNS for row in rows)


@pytest.mark.parametrize("ascii_", [False, True])
def test_vbar_metadata_preserves_resolved_axes_and_excludes_title_and_baseline(ascii_):
    metadata = {}
    charts.vbar_chart(L.Glyphs(ascii_), [0., 50.], 80, 6, title="Memory", sample_times=[100., 200.],
                      metadata=metadata)
    assert metadata["plot_rect"] == (1, 10, 7, 80)
    assert metadata["x_bounds"] == (100., 200.)
    assert metadata["y_bounds"] == (0., 52.5)


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("values", [[0., 100.], [100., 0.]])
def test_temporal_zoom_between_adjacent_samples_retains_the_exact_observed_line(ascii_, values):
    rows, metadata = render(values, ascii_=ascii_, lo=0, hi=100, title="metric",
                            sample_times=[0., 100.], times=(40., 60.), sample_interval=100.)
    measured = ink(rows, metadata, ascii_)
    assert all(any(row[x] for row in measured) for x in range(len(measured[0])))
    # Only the real 40..60 part of the connected line appears, not clamped
    # outside endpoints at 0/100 and not fabricated observations in the header.
    assert not any(measured[0]) and not any(measured[-1])
    assert metadata["x_bounds"] == (40., 60.) and metadata["has_data"]
    assert "awaiting samples" in L.row_text(rows[0])
    points, times = charts._time_points(values, [0., 100.], 11, (40., 60.), 100., True)
    assert times == (40., 60.)
    expected = [40., 60.] if values[0] < values[1] else [60., 40.]
    assert points == [(0, expected[0], False), (10, expected[1], True)]


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("values,interval", [([0., 100.], 1.), ([None, 100.], 100.),
                                            ([0., None], 100.), ([None, None], 100.)])
def test_temporal_clipping_cannot_bridge_missing_endpoints_or_cadence_outages(ascii_, values, interval):
    rows, metadata = render(values, ascii_=ascii_, lo=0, hi=100,
                            sample_times=[0., 100.], times=(40., 60.), sample_interval=interval)
    assert not any(any(row) for row in ink(rows, metadata, ascii_))
    assert metadata["x_bounds"] == (40., 60.) and not metadata["has_data"]


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("times", [(-100., -50.), (150., 200.)])
def test_temporal_zoom_never_extrapolates_before_or_after_observed_trace(ascii_, times):
    rows, metadata = render([0., 100.], ascii_=ascii_, lo=0, hi=100,
                            sample_times=[0., 100.], times=times, sample_interval=100.)
    assert not any(any(row) for row in ink(rows, metadata, ascii_))
    assert metadata["x_bounds"] == times and not metadata["has_data"]


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("painter", [charts.braille_chart, charts.vbar_chart])
def test_window_statistics_include_actual_records_only_and_not_edge_intersections(ascii_, painter):
    metadata = {}
    rows = painter(L.Glyphs(ascii_), [0., 20., 50., 80., 100.], 100, 6,
                   title="Metric", lo=0, hi=100,
                   sample_times=[0., 20., 50., 80., 100.], times=(30., 70.),
                   sample_interval=30., metadata=metadata)
    header = L.row_text(rows[0])
    assert "last 50" in header and "mean 50" in header and "max 50" in header and "min 50" in header
    assert "last 100" not in header and "min 0" not in header
    assert metadata["x_bounds"] == (30., 70.)


def test_temporal_intersections_preserve_missing_inside_window_instead_of_filling_it():
    points, _ = charts._time_points([0., 30., None, 70., 100.], [0., 30., 50., 70., 100.],
                                    21, (20., 80.), 30., True)
    assert points[0] == (0, 20., False) and points[-1] == (20, 80., True)
    middle = next(point for point in points if point[0] == 10)
    assert middle == (10, None, False)
    after = next(point for point in points if point[1] == 70.)
    assert not after[2]


@pytest.mark.parametrize("reverse", [False, True])
def test_temporal_edge_interpolation_avoids_overflow_at_ieee_extremes(reverse):
    maximum = sys.float_info.max
    values = [maximum, -maximum] if reverse else [-maximum, maximum]
    points, _ = charts._time_points(values, [0., 100.], 11, (25., 75.), 100., True)
    assert all(math.isfinite(point[1]) for point in points)
    expected = [maximum / 2, -maximum / 2] if reverse else [-maximum / 2, maximum / 2]
    assert [point[1] for point in points] == pytest.approx(expected)
