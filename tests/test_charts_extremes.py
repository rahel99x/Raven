"""Finite scientific data must retain its meaning across the IEEE-754 range."""
from decimal import Decimal, localcontext
import math
import sys

import pytest

from raven import charts
from raven.layout import Glyphs, row_text, vlen


MAX = sys.float_info.max
TINY = math.ulp(0.0)


@pytest.mark.parametrize("values,expected", [
    ([MAX, MAX], MAX),
    ([-MAX, -MAX], -MAX),
    ([MAX, MAX, -MAX, -MAX], 0.0),
    ([MAX, MAX, -MAX, -MAX, 1e-300], 2e-301),
    ([MAX, MAX, -MAX, -MAX] + [TINY] * 100, TINY),
    ([TINY, TINY], TINY),
    ([0.0, 1e308], 5e307),
])
def test_bucket_mean_preserves_extreme_values_and_cancellation_residuals(values, expected):
    result = charts.resample(values, 1)[0]
    assert math.isfinite(result)
    assert result == expected


@pytest.mark.parametrize("lo,hi", [
    (-MAX, MAX), (-1e308, 1e308),
    (1e308, math.nextafter(1e308, math.inf)),
    (-1e308, math.nextafter(-1e308, math.inf)),
    (-TINY, TINY), (0.0, TINY),
])
def test_chart_coordinates_match_exact_reference_with_pixel_safe_error(lo, hi):
    values = [lo, hi, charts._between(lo, hi, 0.5)]
    with localcontext() as context:
        context.prec = 1100
        lower, upper = Decimal.from_float(lo), Decimal.from_float(hi)
        for value in values:
            expected = float((Decimal.from_float(value) - lower) / (upper - lower))
            result = charts._fraction(value, lo, hi)
            assert math.isfinite(result) and 0 <= result <= 1
            assert result == pytest.approx(expected, abs=3e-16)
    assert charts._fraction(lo, lo, hi) == 0
    assert charts._fraction(hi, lo, hi) == 1


@pytest.mark.parametrize("value", [-MAX, -1e308, -TINY, 0.0, TINY, 1e308, MAX])
def test_automatic_flat_bounds_are_finite_and_strict(value):
    lo, hi = charts._bounds([value, value], value, None)
    assert math.isfinite(lo) and math.isfinite(hi) and hi > lo
    assert lo <= value <= hi


def test_auto_padding_saturates_at_largest_float_and_midpoints_stay_finite():
    lo, hi = charts._bounds([-MAX, MAX], -MAX, None)
    assert lo == -MAX and hi == MAX
    assert charts._mean((lo, hi)) == 0
    assert charts._mean((MAX, MAX)) == MAX


@pytest.mark.parametrize("ascii_", [True, False])
@pytest.mark.parametrize("values,lo,hi", [
    ([-MAX, 0.0, MAX], -MAX, MAX),
    ([-1e308, -1e308], -1e308, None),
    ([MAX, MAX], MAX, None),
    ([0.0, TINY], 0.0, TINY),
    ([1e308, math.nextafter(1e308, math.inf)], 1e308, None),
])
def test_all_numeric_chart_primitives_accept_extreme_science(values, lo, hi, ascii_):
    glyphs = Glyphs(ascii_)
    rows = charts.vbar_chart(glyphs, values, 90, 6, lo=lo, hi=hi, title="Scientific values")
    rows += charts.braille_chart(glyphs, values, 90, 6, lo=lo, hi=hi, title="Scientific values")
    rows += charts.heatmap(glyphs, [values], 90, lo=lo, hi=hi, title="Scientific values")
    rows += charts.hbar_rows(glyphs, [("data", max(values), "cyan")], 90)
    rows += charts.histogram(glyphs, values, [lo, max(values)], 90)
    assert all(vlen(row_text(row)) <= 90 for row in rows)
    text = "\n".join(row_text(row) for row in rows)
    assert "nan" not in text.lower() and "inf" not in text.lower()
    assert text.isascii() if ascii_ else "#" not in text


@pytest.mark.parametrize("ascii_", [True, False])
def test_overflowing_composition_preserves_exact_cell_proportions(ascii_):
    rows = charts.stacked_bar(Glyphs(ascii_), [("first", MAX, "red"), ("second", MAX, "cyan")], 42)
    filled = [(text, style) for text, style in rows[0] if style in {"red", "cyan"}]
    assert [len(text) for text, _ in filled] == [20, 20]
    assert len(row_text(rows[0])) == 42


def test_tiny_composition_is_not_reported_as_zero():
    rows = charts.stacked_bar(Glyphs(False), [("first", TINY, "red"), ("second", TINY, "cyan")], 42)
    assert [len(text) for text, style in rows[0] if style in {"red", "cyan"}] == [20, 20]
    assert "total 0" not in row_text(rows[1])


@pytest.mark.parametrize("times", [(-MAX, MAX), (1.0, 1e308), (1e308, math.nextafter(1e308, math.inf))])
@pytest.mark.parametrize("elapsed", [False, True])
def test_extreme_clock_axes_and_timestamp_buckets_are_finite_and_bounded(times, elapsed):
    axis = charts.time_axis(*times, 70, elapsed=elapsed)
    assert vlen(row_text(axis)) <= 70
    assert "nan" not in row_text(axis).lower() and "inf" not in row_text(axis).lower()
    rows = charts.braille_chart(Glyphs(False), [-1e308, 1e308], 70, 5, lo=-1e308,
                               sample_times=times, elapsed=elapsed)
    assert all(vlen(row_text(row)) <= 70 for row in rows)
    assert any(character in charts.BRAILLE[1:] for row in rows for character in row_text(row)[10:])


def test_timestamp_bucket_mean_preserves_subnormals():
    points, _ = charts._time_points([TINY, TINY], [1.0, 1.0], 1, None, None)
    assert points[0][1] == TINY


def test_gantt_handles_signed_extreme_epochs_without_losing_visible_span():
    rows = charts.gantt(Glyphs(False), [{"id": "7", "state": "COMPLETED", "start": -MAX, "end": MAX}],
                        -MAX, MAX, 90)
    assert any("█" in row_text(row) for row in rows)
    assert all(vlen(row_text(row)) <= 90 for row in rows)


def test_numeric_labels_distinguish_small_measurements_from_zero():
    assert charts.fmt_num(0.0) == "0"
    assert charts.fmt_num(0.001) == "0.001"
    assert charts.fmt_num(TINY) != "0" and len(charts.fmt_num(MAX)) <= 8
    assert charts.fmt_num(50, "%") == "50%"
