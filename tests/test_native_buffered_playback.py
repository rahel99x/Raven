"""Native plots reveal acquired line segments behind a bounded display clock."""
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, charts, clock, layout as L, metric_live as M
from raven.config import Config
from raven.native_series_cache import ObservationIndex
from raven.views import Views, _metric_source_times, _metric_edge_values


def dashboard(ascii_=False):
    app = SimpleNamespace(mode="main", tab="analytics", analytics_job="7", analytics_view="job",
                          selected_id="7", cfg=Config(), theme="dark", width=110, height=40)
    return app, Views(L.Glyphs(ascii_), app.cfg)


def test_source_clock_ignores_future_invalid_and_duplicate_timestamps():
    times = [10., 5., 10., None, float("nan"), True, 7., 30., -1.]
    assert _metric_source_times(times, 11.) == (-1., 10., 7.)
    assert _metric_source_times(times, 6.) == (-1., 5., -1.)
    assert _metric_source_times(times, -2.) == (None, None, None)
    assert _metric_source_times(times, float("inf")) == (None, None, None)


def test_indexed_source_clock_uses_only_logarithmic_timestamp_reads():
    index = ObservationIndex([float(value // 3) for value in range(600_000)])
    reads = []

    class Times:
        def __len__(self):
            return 600_000

        def __getitem__(self, position):
            reads.append(position)
            return float(position // 3)

        def __iter__(self):
            pytest.fail("A moving frame scanned retained timestamps")

    index.sorted_times = Times()
    assert _metric_source_times(Times(), 151_000.5, index) == (0., 151_000., 150_999.)
    assert len(reads) < 50


def test_clipped_edge_peak_survives_gaps_elsewhere_and_duplicate_unknown_values():
    times = [150., 155., 160., 165., 170., 175., 180., 185., 190., 195., 200.]
    values = [1000., 1000., None, 1000., 1000., 1000., None, 1000., 1190., 5000., 1000.]
    assert 2714. == _metric_edge_values(values, times, (162., 192.), 5.)[-1]
    assert _metric_edge_values([None, 1., 2.], [1., 1., 2.], (1.5, 2.), 1.) == [1.5, 2.]


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("filled", [False, True])
@pytest.mark.parametrize("metric,hi", [("cpu-rate", 100.), ("resident-memory", None), ("gpu:0:rate", 100.)])
def test_native_buffer_keeps_successor_and_fills_short_window_between_samples(monkeypatch, ascii_, filled, metric, hi):
    app, views = dashboard(ascii_)
    identity = ("resource-series", "7", metric, "%", "job", "attempt")
    M.set_running(app, identity, True)
    assert M.set_enabled(app, identity, True)
    assert M.set_delta(app, identity, 1.)
    if filled:
        identity = ("resource-area", *identity[1:])
    timer = [101.2]
    monkeypatch.setattr(clock, "now", lambda: timer[0])
    stamps = [float(index * 5) for index in range(22)]  # Last point105 is future.
    values = [30. + index if hi else 1000. + 20 * index for index in range(22)]
    captured, original = [], views._metric_rasters.render

    def render(glyphs, values, *args, **options):
        rows, metadata = original(glyphs, values, *args, **options)
        captured.append((list(values), options, metadata))
        return rows, metadata

    monkeypatch.setattr(views._metric_rasters, "render", render)
    for _ in range(2):
        rows = views.metric_curve(app, values, 110, 7, identity, running=True, filled=filled,
                                  title="Resource", times=(stamps[0], stamps[-1]), sample_times=stamps,
                                  sample_interval=5., observation_index=ObservationIndex(stamps), hi=hi)
        visible, options, metadata = captured[-1]
        left, right = options["times"]
        assert right - left == pytest.approx(1.) and right < timer[0]
        assert max(options["sample_times"]) <= timer[0]
        assert max(options["sample_times"]) > right  # Acquired successor survives.
        points, _ = charts._time_points(visible, options["sample_times"], 100,
                                       options["times"], 5., True)
        assert points[0][0] == 0 and points[-1][0] == 99
        assert all(value is not None for _, value, _ in points)
        assert metadata["has_data"]
        if hi is None:
            assert metadata["y_bounds"][1] > max(value for _, value, _ in points)
        text = L.to_text(rows, 110)
        assert "no observations in live window" not in text
        assert "between acquired samples" in text
        timer[0] += .1
    assert captured[1][1]["times"][1] > captured[0][1]["times"][1]


@pytest.mark.parametrize("ascii_", [False, True])
def test_default_running_full_history_reveals_buffered_end_without_live_toggle(monkeypatch, ascii_):
    app, views = dashboard(ascii_)
    identity = ("resource-series", "7", "cpu-rate", "%", "job", "attempt")
    monkeypatch.setattr(clock, "now", lambda: 101.)
    C.begin_frame(app, 110, 40)
    stamps = [float(index) for index in range(102)]
    rows = views.metric_curve(app, [50.] * len(stamps), 110, 7, identity, running=True,
                              title="CPU", sample_times=stamps, times=(0., 101.),
                              sample_interval=1., hi=100., observation_index=ObservationIndex(stamps))
    C.publish(app, 110, 40)
    assert not M.enabled(app, identity)
    plot, = C.initialize(app)["plots"]
    assert plot.x_bounds[0] == 0. and 0. < plot.x_bounds[1] < 101.
    assert M.playback_label(app, identity, ascii_=ascii_) in L.to_text(rows, 110)


@pytest.mark.parametrize("scope,running", [("resource-series", False), ("resource-compare", None)])
def test_finished_and_comparison_spans_remain_exact(monkeypatch, scope, running):
    app, views = dashboard()
    identity = (scope, "7", "cpu-rate", "%", "job", "attempt")
    monkeypatch.setattr(clock, "now", lambda: 101.)
    C.begin_frame(app, 110, 40)
    views.metric_curve(app, [0., 50., 70.], 110, 7, identity, running=running,
                       title="CPU", sample_times=[0., 95., 100.], times=(0., 100.),
                       sample_interval=5., hi=100.)
    C.publish(app, 110, 40)
    plot, = C.initialize(app)["plots"]
    assert plot.x_bounds == (0., 100.)
    assert M.playback_status(app, identity) is None


def test_buffering_does_not_bridge_missing_samples_or_source_outages(monkeypatch):
    app, views = dashboard()
    identity = ("resource-series", "7", "resident-memory", "G", "job", "attempt")
    M.set_running(app, identity, True)
    M.set_enabled(app, identity, True)
    M.set_delta(app, identity, 1.)
    monkeypatch.setattr(clock, "now", lambda: 101.2)
    C.begin_frame(app, 110, 40)
    rows = views.metric_curve(app, [10., None, 30.], 110, 7, identity, running=True,
                              title="Memory", sample_times=[85., 90., 95.], times=(85., 95.),
                              sample_interval=5., hi=None)
    C.publish(app, 110, 40)
    plot, = C.initialize(app)["plots"]
    assert plot.kind == "metric-empty"
    assert "no observations in live window" in L.to_text(rows, 110)


def test_auto_height_keeps_measured_peaks_when_coarse_edge_buckets_include_gaps(monkeypatch):
    app, views = dashboard()
    identity = ("resource-series", "7", "resident-memory", "G", "job", "attempt")
    M.set_running(app, identity, True)
    M.set_enabled(app, identity, True)
    monkeypatch.setattr(clock, "now", lambda: 101.2)
    stamps = [float(index * 5) for index in range(21)]
    values = [None if index % 2 else 1000. for index in range(21)]
    C.begin_frame(app, 110, 40)
    views.metric_curve(app, values, 110, 7, identity, running=True, title="Memory",
                       sample_times=stamps, times=(0., 100.), sample_interval=5., hi=None)
    C.publish(app, 110, 40)
    plot, = C.initialize(app)["plots"]
    assert plot.kind == "metric"
    assert plot.y_bounds[1] > 1000.


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("view", ["full", "live", "zoom"])
def test_native_rebuilt_measurement_headers_identify_visible_fit_and_range(monkeypatch, ascii_, view):
    app, views = dashboard(ascii_)
    identity = ("resource-series", "7", "cpu-rate", "%", "job", "attempt")
    M.set_running(app, identity, True)
    if view == "live":
        M.set_enabled(app, identity, True)
    elif view == "zoom":
        monkeypatch.setattr(C, "bounds", lambda *args, **kwargs: {"x": (20., 60.), "y": (0., 100.)})
    monkeypatch.setattr(clock, "now", lambda: 101.)
    values = [10., 90.] * 2000
    times = [index / 40. for index in range(len(values))]
    rows = views.metric_curve(app, values, 90, 8, identity, running=True,
                              title="A long resource name that cannot fully fit in the displayed column",
                              sample_times=times, times=(times[0], times[-1]), hi=100.,
                              sample_interval=.025, observation_index=ObservationIndex(times))
    header = L.row_text(rows[1])  # Running source controls occupy row zero.
    assert "[fit + range]" in header
    assert "last " in header and "90" in header
    assert L.vlen(header) <= 90
    if ascii_:
        assert header.isascii()


@pytest.mark.parametrize("ascii_", [False, True])
def test_native_sparse_between_sample_header_does_not_claim_an_unrendered_fit(monkeypatch, ascii_):
    app, views = dashboard(ascii_)
    identity = ("resource-series", "7", "cpu-rate", "%", "job", "attempt")
    M.set_running(app, identity, True)
    M.set_enabled(app, identity, True)
    M.set_delta(app, identity, 1.)
    monkeypatch.setattr(clock, "now", lambda: 101.2)
    times = [float(index * 5) for index in range(21)]
    rows = views.metric_curve(app, [50.] * len(times), 90, 8, identity, running=True,
                              title="CPU", sample_times=times, times=(0., 100.), hi=100.,
                              sample_interval=5., observation_index=ObservationIndex(times))
    header = L.row_text(rows[1])
    assert "between acquired samples" in header
    assert "[fit" not in header and "[range" not in header
