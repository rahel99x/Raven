"""Small live windows retain real clock anchors and readable fractional seconds."""
import math
import time

import pytest

from raven import charts, layout as L


def labels(start, end, width=80, **kwargs):
    return L.row_text(charts.time_axis(start, end, width, **kwargs)).split()


@pytest.mark.parametrize("span", [.5, .1, .01, .001])
@pytest.mark.parametrize("width", [30, 50, 80, 120])
def test_subsecond_axis_keeps_two_distinct_actual_clock_endpoints_when_they_fit(span, width):
    start = 1700000000.
    found = labels(start, start + span, width)
    assert len(found) >= 2 and found[0] != found[-1]
    assert all(label.count(":") == 2 and "." in label for label in found)
    assert all(1 <= len(label.split(".")[-1]) <= 6 for label in found)
    assert found[0].startswith(time.strftime("%H:%M:%S", time.localtime(start)))
    assert found[-1].startswith(time.strftime("%H:%M:%S", time.localtime(start + span)))
    assert "now" not in " ".join(found)


@pytest.mark.parametrize("width", [0, 1, 3, 8, 9, 12, 20, 30, 50, 80, 120, 2048])
@pytest.mark.parametrize("elapsed", [False, True])
def test_fractional_axis_has_no_collisions_and_respects_geometry(width, elapsed):
    rows = charts.time_axis(10.0005, 10.0015, width, indent="  ", elapsed=elapsed)
    line = L.row_text(rows)
    assert L.vlen(line) == width + 2
    assert all(label.isascii() for label in line.split())
    assert len(line.split()) <= 8
    if width > 25:
        assert len(line.split()) == len(set(line.split()))


@pytest.mark.parametrize("elapsed", [False, True])
def test_one_millisecond_interval_is_resolved_without_inventing_new_sampling_resolution(elapsed):
    text = L.row_text(charts.time_axis(100., 100.001, 80, elapsed=elapsed))
    parts = text.split()
    assert len(parts) >= 2
    assert parts[0].endswith(".0000" + ("s" if elapsed else ""))
    assert parts[-1].endswith(".0010" + ("s" if elapsed else ""))


@pytest.mark.parametrize("span", [.001, .0001, .00001, .000001])
def test_fractional_precision_never_exceeds_six_decimal_places(span):
    found = labels(100., 100. + span, 120)
    assert all(1 <= len(label.split(".")[-1]) <= 6 for label in found)


def test_second_boundary_rounding_carries_the_clock_instead_of_printing_an_invalid_fraction():
    before = 1700000000.999996
    found = labels(before, before + .0002, 80)
    assert found[0].startswith(time.strftime("%H:%M:%S", time.localtime(math.floor(before) + 1)))
    assert ".10000" not in " ".join(found)


def test_midnight_rounding_uses_actual_next_date_clock():
    midnight = time.mktime((2026, 10, 9, 0, 0, 0, 0, 0, -1))
    found = labels(midnight - .000004, midnight + .000196, 80)
    assert all(label.startswith("00:00:00.") for label in found)
    assert found[0] != found[-1]


@pytest.mark.parametrize("start,end", [(-.001, .001), (math.ulp(0.), math.ulp(0.) * 2),
                                        (0., 1e-310), (1e-6, 2e-6)])
@pytest.mark.parametrize("elapsed", [False, True])
def test_tiny_or_negative_epoch_intervals_do_not_overflow_or_emit_invalid_values(start, end, elapsed):
    text = L.row_text(charts.time_axis(start, end, 80, elapsed=elapsed))
    assert L.vlen(text) == 80
    assert "nan" not in text.lower() and "inf" not in text.lower()


@pytest.mark.parametrize("start,end", [(math.nan, 1.), (0., math.inf), (1., 1.), (2., 1.)])
def test_invalid_or_flat_clock_axes_remain_blank(start, end):
    assert L.row_text(charts.time_axis(start, end, 80)) == " " * 80


def test_legacy_whole_second_short_span_retains_single_full_second_clock():
    assert len(labels(100., 105., 80)) == 1
    assert labels(100., 105., 80)[0] == time.strftime("%H:%M:%S", time.localtime(105.))


def test_elapsed_subsecond_ticks_show_original_duration_instead_of_wallclock():
    found = labels(10000.0005, 10000.0015, 80, elapsed=True)
    assert found[0].startswith("10000.0005") and found[-1].startswith("10000.0015")
    assert all(label.endswith("s") and ":" not in label for label in found)
