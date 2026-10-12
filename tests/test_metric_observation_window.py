"""Live-window pruning retains the exact measured curve, gaps, and statistics."""
import math
import random
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, charts, clock, layout as L, metric_live as M
from raven.config import Config
from raven.views import Views, _metric_observations
from raven.native_series_cache import ObservationIndex


@pytest.mark.parametrize("envelope", [False, True])
@pytest.mark.parametrize("bounds", [(4., 8.), (4.1, 4.2), (2., 2.), (-20., -10.), (50., 60.)])
@pytest.mark.parametrize("interval", [.2, 1., 20.])
def test_pruned_edge_samples_preserve_exact_raster_vertices(envelope, bounds, interval):
    # Duplicate timestamps, unsorted arrival, missing endpoints, and a long
    # producer outage must all keep their original connection decisions.
    times = [0., 4., 1., 3., 3., 3., 8., 6., 6., 20., math.nan, True]
    values = [0., 90., 50., None, 60., 40., 30., None, 80., 100., 20., 30.]
    kept, stamps, visible, newest = _metric_observations(values, times, bounds)
    assert charts._time_points(kept, stamps, 79, bounds, interval, envelope) == charts._time_points(
        values, times, 79, bounds, interval, envelope)
    assert visible == [charts._finite(value) for value, timestamp in zip(values, times)
                       if charts._finite(timestamp) is not None and bounds[0] <= timestamp <= bounds[1]]
    assert newest == 20.
    assert sum(timestamp < bounds[0] for timestamp in stamps) <= 1
    assert sum(timestamp > bounds[1] for timestamp in stamps) <= 1


@pytest.mark.parametrize("seed", range(30))
def test_pruning_matches_full_observations_with_duplicate_edges_and_future_records(seed):
    source = random.Random(seed)
    pairs = [(source.randrange(-30, 31) / 2, source.choice([None, source.uniform(-100, 100)]))
             for _ in range(80)]
    source.shuffle(pairs)
    times, values = zip(*pairs)
    bounds, latest = (source.uniform(-8, -1), source.uniform(1, 8)), 10.
    kept, stamps, visible, newest = _metric_observations(values, times, bounds, latest=latest)
    original = [(value, timestamp) for value, timestamp in zip(values, times) if timestamp <= latest]
    for width in (1, 9, 53):
        for envelope in (False, True):
            assert charts._time_points(kept, stamps, width, bounds, .5, envelope) == charts._time_points(
                [value for value, _ in original], [timestamp for _, timestamp in original],
                width, bounds, .5, envelope)
    assert newest == max(timestamp for _, timestamp in original)
    assert visible == [value for value, timestamp in original if bounds[0] <= timestamp <= bounds[1]]


@pytest.mark.parametrize("filled", [False, True])
@pytest.mark.parametrize("ascii_", [False, True])
def test_pruned_and_complete_rasters_match_at_epoch_and_fine_intersections(filled, ascii_):
    epoch = 1_790_000_000.
    times = [epoch + index * .001 for index in range(100)]
    values = [50 + 40 * math.sin(index / 4) if index % 9 else None for index in range(100)]
    bounds = (epoch + .0305, epoch + .0385)
    kept, stamps, _, _ = _metric_observations(values, times, bounds)
    painter, g = (charts.vbar_chart if filled else charts.braille_chart), L.Glyphs(ascii_)
    full_meta, kept_meta = {}, {}
    options = dict(times=bounds, sample_interval=.001, lo=0., hi=100., fitted=True, time_units=True)
    assert painter(g, kept, 84, 9, sample_times=stamps, metadata=kept_meta, **options) == painter(
        g, values, 84, 9, sample_times=times, metadata=full_meta, **options)
    assert kept_meta == full_meta


@pytest.mark.parametrize("interval", [None, 0., math.nan])
def test_no_explicit_cadence_keeps_full_history_for_median_gap_inference(monkeypatch, interval):
    app = SimpleNamespace(mode="main", tab="analytics", analytics_job="7", analytics_view="job",
                          selected_id="7", cfg=Config(), theme="dark", width=100, height=30)
    identity = ("resource-series", "7", "cpu-rate", "%", "job", "attempt")
    M.set_delta(app, identity, 1.)
    M.set_running(app, identity, True)
    assert M.set_enabled(app, identity, True)
    monkeypatch.setattr(clock, "now", lambda: 10.)
    views = Views(L.Glyphs(False), app.cfg)
    recorded = []
    original = views._metric_rasters.render
    monkeypatch.setattr(views._metric_rasters, "render", lambda glyphs, values, *args, **kwargs:
                        (recorded.append((list(values), kwargs["sample_times"])),
                         original(glyphs, values, *args, **kwargs))[1])
    values, times = [1., 2., 3., 4.], [1., 2., 9.5, 10.]
    views.metric_curve(app, values, 100, 8, identity, sample_times=times, times=(1., 10.),
                       sample_interval=interval)
    assert recorded == [(values, times)]


@pytest.mark.parametrize("captured", [False, True])
@pytest.mark.parametrize("filled", [False, True])
def test_indexed_live_zoom_and_capture_read_only_window_and_exclude_future_records(monkeypatch, captured, filled):
    app = SimpleNamespace(mode="main", tab="analytics", analytics_job="7", analytics_view="job",
                          selected_id="7", cfg=Config(), theme="dark", width=100, height=30)
    identity = ("resource-series", "7", "cpu-rate", "%", "job", "attempt")
    M.set_delta(app, identity, 1.)
    M.set_running(app, identity, True)
    assert M.set_enabled(app, identity, True)
    monkeypatch.setattr(clock, "now", lambda: 9.7)
    bounds = {"x": (9.2, 9.8), "y": (0., 100.)}
    monkeypatch.setattr(C, "bounds", lambda *args, **kwargs: bounds)
    monkeypatch.setattr(C, "autofit", lambda *args, **kwargs: True)
    monkeypatch.setattr(C, "captured_bounds", lambda *args, **kwargs: bounds if captured else None)
    views = Views(L.Glyphs(False), app.cfg)
    samples, stamps = [float(index % 100) for index in range(10000)], [index * .001 for index in range(10000)]
    expected = views.metric_curve(app, samples, 100, 8, identity, filled=filled,
                                 sample_times=stamps, times=(0., 10.), sample_interval=.001)
    index, reads = ObservationIndex(stamps), []

    class WindowValues:
        def __getitem__(self, item):
            reads.append(item)
            return samples[item]

        def __iter__(self):
            pytest.fail("An indexed Live zoom or capture scanned the entire retained history")

    monkeypatch.setattr("raven.views._metric_observations", lambda *args, **kwargs:
                        pytest.fail("An indexed Live curve performed a full observation scan"))
    rendered, original = [], views._metric_rasters.render

    def render(glyphs, values, *args, **kwargs):
        rendered.append((len(values), max(kwargs["sample_times"])))
        return original(glyphs, values, *args, **kwargs)

    monkeypatch.setattr(views._metric_rasters, "render", render)
    actual = views.metric_curve(app, WindowValues(), 100, 8, identity, filled=filled,
                                sample_times=stamps, times=(0., 10.), sample_interval=.001,
                                observation_index=index)
    assert actual == expected
    assert rendered and rendered[0][0] <= 502 and rendered[0][1] <= 9.7
    # One retained interval for paint, plus one for fitting when not held.
    assert len(reads) <= (2004 if not captured else 1002)
