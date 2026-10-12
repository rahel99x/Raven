"""Full-history presentation reuses rasters until measured edge geometry changes."""
from types import SimpleNamespace

import pytest

from raven import charts, chart_interaction as C, clock, layout as L, metric_live as M
from raven.config import Config
from raven.native_series_cache import ObservationIndex
from raven.views import Views, _metric_edge_signature


@pytest.fixture
def native(monkeypatch):
    app = SimpleNamespace(mode="main", tab="analytics", analytics_job="7", analytics_view="job",
                          selected_id="7", cfg=Config(), theme="dark", width=120, height=40)
    views = Views(L.Glyphs(False), app.cfg)
    identity = ("resource-series", "7", "cpu-rate", "%", "job", "attempt")
    timer = [20000.1]
    monkeypatch.setattr(clock, "now", lambda: timer[0])
    times = [float(index * 5) for index in range(4001)]
    values = [50.] * len(times)
    index = ObservationIndex(times)
    calls, original = [], charts.braille_chart

    def painter(*args, **kwargs):
        calls.append(dict(kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(charts, "braille_chart", painter)

    def draw(*, width=120, height=8, hi=100., key=identity, filled=False):
        C.begin_frame(app, width, 40)
        rows = views.metric_curve(app, values, width, height, key, running=not filled, filled=filled,
                                  title="CPU", hi=hi, sample_times=times, times=(times[0], times[-1]),
                                  sample_interval=5., observation_index=index)
        C.publish(app, width, 40)
        return rows, C.initialize(app)["plots"][0]

    return SimpleNamespace(app=app, views=views, identity=identity, timer=timer, values=values,
                           times=times, index=index, calls=calls, draw=draw)


def test_quiet_maintenance_reuses_long_history_raster_and_reports_actual_painted_lag(native):
    d = native
    _, first = d.draw()
    assert len(d.calls) == 1
    for _ in range(9):
        d.timer[0] += .2
        rows, current = d.draw()
        assert current.x_bounds == first.x_bounds
        assert M.playback_label(d.app, d.identity, displayed_end=current.x_bounds[1]) in L.to_text(rows, 120)
    assert len(d.calls) == 1
    assert M.playback_status(d.app, d.identity).end > first.x_bounds[1]


@pytest.mark.parametrize("hi", [100., None])
def test_vertical_edge_motion_and_auto_height_refresh_before_one_horizontal_subcell(native, hi):
    d = native
    d.values[-3], d.values[-2] = 0., 100.
    _, first = d.draw(hi=hi)
    d.timer[0] += .5
    _, second = d.draw(hi=hi)
    assert len(d.calls) == 2
    assert second.x_bounds[1] > first.x_bounds[1]
    if hi is None:
        # Older observations hold the 50% maximum. Repeat with a larger edge
        # so its genuinely interpolated peak now controls the automatic axis.
        d.values[-2] = 10000.
        _, third = d.draw(hi=None)
        assert third.y_bounds[1] > second.y_bounds[1]
        assert len(d.calls) == 3


@pytest.mark.parametrize("change", ["interior-correction", "missing-edge", "edge-correction", "resize", "height", "theme", "rate"])
def test_data_and_geometry_changes_are_never_hidden_by_presentation_reuse(native, change):
    d = native
    _, before = d.draw()
    d.timer[0] += .2
    options = {}
    if change == "interior-correction":
        d.values[2000] = 99.
    elif change == "missing-edge":
        d.values[-2] = None
    elif change == "edge-correction":
        d.values[-2] = 90.
    elif change == "resize":
        options["width"] = 121
    elif change == "height":
        options["height"] = 10
    elif change == "theme":
        d.app.theme = "gruvbox-dark"
    else:
        assert M.set_rate(d.app, d.identity, 100)
    _, after = d.draw(**options)
    assert len(d.calls) == 2
    if change != "interior-correction":
        assert after.x_bounds[1] > before.x_bounds[1]


def test_crossing_a_measured_spike_publishes_even_if_its_raster_column_is_shared(native):
    d = native
    d.values[-2] = 99.
    _, first = d.draw()
    # Advance across an acquired timestamp. The whole long-history X domain
    # still moves less than one cell; the newly revealed vertex must not wait.
    d.timer[0] = 20005.1
    _, second = d.draw()
    assert second.x_bounds[1] > 19995. and second.x_bounds[1] > first.x_bounds[1]
    assert len(d.calls) == 2


def test_live_mode_bypasses_full_history_gate_and_returning_resets_it(native):
    d = native
    _, full = d.draw()
    assert M.set_enabled(d.app, d.identity, True)
    _, live = d.draw()
    d.timer[0] += .2
    _, next_live = d.draw()
    assert next_live.x_bounds[1] > live.x_bounds[1]
    assert M.set_enabled(d.app, d.identity, False)
    _, restored = d.draw()
    assert restored.x_bounds[0] == full.x_bounds[0]
    assert restored.x_bounds[1] == M.playback_status(d.app, d.identity).end


def test_companion_area_uses_the_curves_actual_polling_clock(native):
    d = native
    assert d.app.cfg["intervals"]["live"] != .5
    d.draw()
    assert M.set_rate(d.app, d.identity, 100)
    assert M.set_enabled(d.app, d.identity, True)
    _, curve = d.draw()
    area = ("resource-area", *d.identity[1:])
    _, companion = d.draw(key=area, filled=True)
    assert companion.x_bounds == curve.x_bounds


def test_indexed_edge_check_reads_only_the_two_acquired_counters(native):
    d = native
    reads = []

    class Values:
        def __getitem__(self, position):
            reads.append(position)
            return d.values[position]

        def __iter__(self):
            pytest.fail("The presentation edge check scanned all history counters")

    signature = _metric_edge_signature(Values(), d.times, 19990.1, 20000.1, 5., 0., 100., 32, d.index)
    assert len(reads) == 2 and signature[-2] == "segment"


def test_short_forward_replay_seek_forces_exact_new_published_domain(native):
    d = native
    replay = SimpleNamespace(generation=1)
    d.app.replay = SimpleNamespace(clock=replay)
    _, first = d.draw()
    d.timer[0] += .2
    _, still = d.draw()
    assert still.x_bounds == first.x_bounds and len(d.calls) == 1
    replay.generation += 1
    _, sought = d.draw()
    assert sought.x_bounds[1] == pytest.approx(19990.3)
    assert sought.x_bounds[1] > first.x_bounds[1] and len(d.calls) == 2


def test_rate_request_change_forces_presentation_even_if_effective_interval_is_capped(native, monkeypatch):
    d = native
    monkeypatch.setattr("raven.metric_sampling.cadence", lambda *args: 5.)
    _, first = d.draw()
    d.timer[0] += .2
    _, still = d.draw()
    assert still.x_bounds == first.x_bounds and len(d.calls) == 1
    assert M.set_rate(d.app, d.identity, 2)
    _, changed = d.draw()
    assert changed.x_bounds[1] > first.x_bounds[1] and len(d.calls) == 2
