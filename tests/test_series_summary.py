"""Indexed envelopes retain exact extrema, order, gap fallback and revisions."""
from hypothesis import given, settings, strategies as st
from raven import charts, metric_raster
from raven.series_summary import EnvelopeIndex


@given(st.lists(st.integers(-100, 100), min_size=2, max_size=100), st.integers(1, 40))
@settings(max_examples=80, deadline=None)
def test_index_matches_existing_renderer_vertices_and_details(values, width):
    times = [float(index) for index in range(len(values))]
    index = EnvelopeIndex(values, times)
    expected_details, actual_details = {}, {}
    expected = charts._time_points(values, times, width, (times[0], times[-1]), 1., True, expected_details)
    actual = index.points(width, (times[0], times[-1]), 1., details=actual_details)
    assert actual == expected and actual_details == expected_details


def test_unknown_outage_duplicate_and_interpolated_edges_use_original_path():
    assert not EnvelopeIndex([1., None], [1., 2.]).valid
    assert not EnvelopeIndex([1., 2.], [1., 1.]).valid
    assert EnvelopeIndex([1., 2., 3.], [1., 2., 20.]).points(5, None, 1.) is None
    assert EnvelopeIndex([1., 2., 3.], [1., 2., 3.]).points(5, (1.5, 3.), 1.) is None


def test_corrected_interior_value_invalidates_cached_index():
    values = [float(index % 9) for index in range(2048)]
    times = [float(index) for index in range(2048)]
    cache = metric_raster.TimePointCache()
    prepare = cache.bind(values, times, metric_raster._freeze(values))
    first = prepare(values, times, 80, (0., 2047.), 1., True)
    values[1000] = 1000000.
    prepare = cache.bind(values, times, metric_raster._freeze(values))
    second = prepare(values, times, 80, (0., 2047.), 1., True)
    assert max(point[1] for point in first[0]) < 1000000.
    assert max(point[1] for point in second[0]) == 1000000.
    assert second == charts._time_points(values, times, 80, (0., 2047.), 1., True)
