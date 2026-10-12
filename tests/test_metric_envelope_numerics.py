"""Independent numerical and discontinuity checks for display-only modelling."""

import math
import random
import sys

import pytest

from raven import charts, metric_envelope as E


def summarized(values, width=None):
    width = len(values) if width is None else width
    details = {}
    points = charts.envelope_points(values, width, details=details)
    return E.columns(points, width, details)


@pytest.mark.parametrize("function", [lambda x: 3.0, lambda x: 2.5 * x - 7,
                                       lambda x: .75 * x * x + 2 * x - 6])
def test_normalized_local_model_reproduces_constant_linear_and_quadratic(function):
    source = summarized([function(x) for x in range(31)])
    trend = E.local_trend(source, 31)
    assert len(trend) == 31
    for x, value, bridge in trend:
        assert value == pytest.approx(function(x), rel=1e-12, abs=1e-12)
        assert bridge is (x > 0)


def test_sparse_quadratic_evaluations_stay_inside_received_domain():
    points = [(x, float(x * x), bool(x)) for x in range(2, 32, 3)]
    trend = E.local_trend(E.columns(points, 40), 40)
    assert trend[0][0] == 2 and trend[-1][0] == 29
    assert len(trend) == 28
    for x, value, _ in trend:
        assert value == pytest.approx(x * x, rel=1e-12, abs=1e-12)


def test_equally_spaced_unknown_column_is_not_bridged_by_a_model():
    trend = E.local_trend(summarized([1.0] * 5 + [None] + [2.0] * 5), 11)
    assert [x for x, _, _ in trend] == list(range(5)) + list(range(6, 11))
    assert dict((x, bridge) for x, _, bridge in trend)[6] is False
    assert all(value == (1 if x < 5 else 2) for x, value, _ in trend)


def test_unknown_dense_bucket_preserves_the_complete_break():
    values = [1.0] * 25 + [2.0, 3.0, None, 4.0, 5.0] + [2.0] * 25
    trend = E.local_trend(summarized(values, 11), 11)
    assert 5 not in [x for x, _, _ in trend]
    assert dict((x, bridge) for x, _, bridge in trend)[6] is False


def test_explicit_continuity_break_splits_neighbourhoods():
    points = [(x, 1.0 if x < 5 else 100.0, x not in (0, 5)) for x in range(10)]
    trend = E.local_trend(E.columns(points, 10), 10)
    assert all(value == (1.0 if x < 5 else 100.0) for x, value, _ in trend)
    assert dict((x, bridge) for x, _, bridge in trend)[5] is False


def test_duplicate_timestamp_steps_do_not_become_smooth_transitions():
    values = [1.0] * 6 + [10.0] * 6
    times = list(range(6)) + list(range(5, 11))
    details = {}
    points, _ = charts._time_points(values, times, 11, (0, 10), 1.0,
                                    envelope=True, details=details)
    source = E.columns(points, 11, details)
    assert details[5]["step"] is True
    trend = E.local_trend(source, 11)
    assert 5 not in [x for x, _, _ in trend]
    assert all(value == (1.0 if x < 5 else 10.0) for x, value, _ in trend)


@pytest.mark.parametrize("epoch", [0.0, 1_700_000_000.0, 4_000_000_000.0])
def test_timestamp_epoch_does_not_change_conditioning(epoch):
    offsets = [i / 1024 for i in range(101)]
    times = [epoch + t for t in offsets]
    values = [float((i - 25) ** 2) for i in range(101)]
    points, _ = charts._time_points(values, times, 31, (times[0], times[-1]),
                                    1 / 1024, envelope=True)
    expected_points, _ = charts._time_points(values, offsets, 31,
                                              (offsets[0], offsets[-1]),
                                              1 / 1024, envelope=True)
    assert points == expected_points
    assert E.local_trend(E.columns(points, 31), 31) == E.local_trend(
        E.columns(expected_points, 31), 31)


@pytest.mark.parametrize("values", [
    [sys.float_info.max] * 21,
    [-sys.float_info.max] * 21,
    [(-1 if i % 2 else 1) * sys.float_info.max for i in range(21)],
    [float.fromhex("0x0.0000000000001p-1022") * (i - 10) for i in range(21)],
    [1e300 + i * 1e286 for i in range(21)],
])
def test_extreme_finite_values_remain_finite_and_inside_adjacent_actual_ranges(values):
    source = summarized(values)
    trend = E.local_trend(source, len(values))
    assert len(trend) == len(values)
    for x, value, _ in trend:
        pair = source[x:x + 2] if x < len(source) - 1 else source[-2:]
        assert math.isfinite(value)
        assert min(item.low for item in pair) <= value <= max(item.high for item in pair)


def test_dense_oscillation_band_keeps_exact_observed_extrema():
    source = summarized([-8.0, 12.0, -7.0, 11.0] * 11, 11)
    band = E.band_columns(source, -10.0, 15.0, 40)
    assert len(band) == 11
    assert all(item.low == -8.0 and item.high == 12.0 for item in band)
    assert all(item.count == 4 and item.turns >= 2 for item in band)


def test_monotone_slopes_and_single_outliers_do_not_get_oscillation_bands():
    for values in (list(range(55)), [0.0] * 25 + [10.0] + [0.0] * 29):
        assert not E.band_columns(summarized(values, 11), 0.0, 54.0, 40)


def test_missing_and_equal_time_steps_never_get_bands():
    source = tuple(E.Column(x, -10.0, 10.0, -10.0, 10.0, 40, True,
                            broken=(x % 2 == 0), turns=20, step=(x % 2 == 1))
                   for x in range(21))
    assert not E.band_columns(source, -10.0, 10.0, 40)
    assert not E.local_trend(source, 21)


def test_repeated_backward_runs_cannot_expand_result_beyond_screen_width():
    source = tuple(E.Column(x, 1.0, 1.0, 1.0, 1.0, 1, bool(x))
                   for _ in range(30) for x in range(11))
    result = E.local_trend(source, 11)
    assert len(result) <= 11
    assert len({x for x, _, _ in result}) == len(result)
    points = [(item.x, item.low, item.bridge) for item in source]
    assert len(E.columns(points, 11)) <= 11


@pytest.mark.parametrize("value", [None, True, False, "bad", math.inf, -math.inf,
                                     math.nan, 10**1000])
def test_bad_observations_are_gaps_instead_of_solver_input(value):
    trend = E.local_trend(summarized([1.0] * 5 + [value] + [2.0] * 5), 11)
    assert 5 not in [x for x, _, _ in trend]
    assert all(math.isfinite(item) for _, item, _ in trend)


def test_random_sparse_extreme_windows_are_bounded_without_overshoot():
    rng = random.Random(9401)
    for _ in range(100):
        xs = sorted(rng.sample(range(128), rng.randrange(5, 40)))
        values = [rng.choice((-1e308, -1e100, -.1, 0., .1, 1e100, 1e308)) for _ in xs]
        source = E.columns([(x, y, bool(i)) for i, (x, y) in enumerate(zip(xs, values))], 128)
        trend = E.local_trend(source, 128)
        assert len(trend) <= 128
        assert all(xs[0] <= x <= xs[-1] and math.isfinite(value) for x, value, _ in trend)
        segment = 0
        for x, value, _ in trend:
            while segment < len(source) - 2 and x >= source[segment + 1].x:
                segment += 1
            pair = source[segment:segment + 2]
            assert min(item.low for item in pair) <= value <= max(item.high for item in pair)
